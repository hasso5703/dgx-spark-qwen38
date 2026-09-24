#!/usr/bin/env bash
# Qwen3.8 serving stack on DGX Spark (GB10): 27B (SGLang+DFlash2) or Flash-Next
# 176B (SGLang+NEXTN, PLE table mmap-served from NVMe). Systemd, hardened.
# Idempotent: safe to re-run at any time (uses local caches when present).
# Everything is PINNED to versions validated on this hardware (the 27B image on
# 2026-09-17, everything else on 2026-09-11); override with env vars if you want
# to try newer builds (see --help). Since v1.14 BOTH lanes serve an OFFICIAL
# SGLang image with nothing added and this script builds nothing by default: the
# flash lane crossed over in v1.8, the 27B lane followed once the two reasons it
# had stayed behind were measured rather than restated (see the IMAGE pin below).
# OVERLAY_FLASH=1 rebuilds the flash lane's old local image, the one rollback
# this repo still carries (see flash-sglang/ATTRIBUTION.md).
set -euo pipefail
trap 'printf "\n\033[1;31mInstall failed at line %s (command: %s).\033[0m\nRe-running ./install.sh is safe: completed steps are skipped.\n" "$LINENO" "$BASH_COMMAND" >&2' ERR

# die() is the first thing this file defines, not something down at the steps:
# the refusal below is the first thing that can reject an invocation, and
# calling a function the shell has not seen yet printed "command not found" and
# exited through the ERR trap, losing the message that names the problem
# (tests/test_install_preflight.py pins that failure mode on the port checks).
die()  { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ── The first wall: this installer does not run as root ──────────────────────
# Everything it writes is addressed from $HOME, and the units it renders carry
# those paths plus User=$(id -un). Put sudo in front and $HOME is /root: the
# config dir becomes /root/.config/qwen38 with a BRAND NEW API key, the engine
# unit points --api-key at that file, and every client reading
# ~/.config/qwen38/api-key gets 401 from an engine that is otherwise perfectly
# healthy. Nothing fails loudly, which is what makes it expensive: on the
# reference box, 2026-09-13, `curl .../get.sh | sudo bash` installed, started,
# served, and answered a key nobody had; it took a read of the sudo audit log
# to see why. This installer calls sudo itself, for the privileged steps and
# for nothing else, so there is never a reason to put sudo in front of it.
# (id -u rather than $EUID: the refusal is testable that way, and a PATH that
# can lie about id could edit this file anyway.)
if [ "$(id -u)" = "0" ]; then
  if [ -n "${SUDO_USER:-}" ]; then
    # || true, and it is load bearing: getent exits 2 on an unknown user, and
    # under set -e + pipefail that killed this very refusal through the ERR
    # trap, printing "Install failed at line ..." instead of the message. A
    # refusal that cannot survive its own lookup is not a refusal.
    SUDO_HOME="$(getent passwd "$SUDO_USER" 2>/dev/null | cut -d: -f6 || true)"
    die "do not put sudo in front of this installer.

  you ran     : sudo ...   (your login is $SUDO_USER)
  run instead : curl -fsSL https://raw.githubusercontent.com/hasso5703/dgx-spark-qwen38/main/get.sh | bash
  or locally  : ./install.sh      (it calls sudo itself, for the steps that need it)

Under sudo, HOME is /root: the API key, the chat template and the compile cache
land in /root/.config/qwen38 and the units point there. The engine then installs,
starts and serves normally with a key nobody has, and every client reading
${SUDO_HOME:-/home/$SUDO_USER}/.config/qwen38/api-key gets 401 from it."
  fi
  [ "${ALLOW_ROOT:-0}" = "1" ] || die "this installer runs as the user who will use the box, not as root.

Everything it writes is addressed from \$HOME (~/.config/qwen38), and the units it
renders carry that path plus User=$(id -un). As root that is /root, which is not
where your clients, your cockpit or your opencode config look.

  log in as that user and run it there
  or, if this box genuinely has no other user: ALLOW_ROOT=1 ./install.sh"
fi

# Remember which knobs the operator set explicitly on THIS invocation, before
# the defaults below fill them in: an explicit env var beats the installed
# unit, which beats the defaults (see the convergence block further down).
_ENV_MODEL_CHOICE="${MODEL_CHOICE:-}"; _ENV_MODEL_REV="${MODEL_REV:-}"
_ENV_PORT="${PORT:-}"; _ENV_HF_CACHE="${HF_CACHE:-}"
_ENV_CONTEXT_MODE="${CONTEXT_MODE:-}"; _ENV_PROXY_PORT="${PROXY_PORT:-}"
_ENV_ENGINE_BIND="${ENGINE_BIND:-}"; _ENV_PROXY_BIND="${PROXY_BIND:-}"
_ENV_PLE_DIR="${PLE_DIR:-}"; FLASH_TIER_ENV="${FLASH_TIER:-}"
_ENV_SERVE_IMAGE="${SERVE_IMAGE:-}"; _ENV_FLASH_SERVE_IMAGE="${FLASH_SERVE_IMAGE:-}"
_ENV_DRAFT2_REPO="${DRAFT2_REPO:-}"; _ENV_DRAFT2_REV="${DRAFT2_REV:-}"
_ENV_DRAFT2_QUANT="${DRAFT2_QUANT:-}"; _ENV_DRAFT2_TOKENS="${DRAFT2_TOKENS:-}"

# ── Pinned, validated versions (override via env if you know what you do) ──
# The 27B lane's image, and since v1.14 it is the official release, served
# directly with no overlay built on top of it. Until v1.13 this lane built its
# own image (the 2026-08-15 base plus eight sha256-verified files) for two
# reasons, and both were retired by measurement on 2026-09-17 rather than by
# assumption:
#   - DFlash2 itself, merged upstream 2026-08-19 (sglang#35371) together with
#     the quantized target lm_head the candidate selector projects through
#     (sglang#35496). Both are in v0.5.19: diffed file by file against the
#     overlay, not one function of it is missing upstream, and upstream carries
#     about ten this box never had.
#   - The mrope fix (sglang#34446, "the fused Qwen3.5 RoPE kernel discards mrope
#     height and width", merged 2026-08-30), whose failure mode is silent image
#     corruption. v0.5.19 is built after it: checked in the image, its
#     fused_qk_rmsnorm_rope_gate.py carries mrope_axis_map, same as the overlay.
# The sm_121 objection that kept this lane off the release is retired too, and it
# was never a departure: v0.5.19 reports arch_list up to sm_120 and ships
# sgl_kernel variants for sm90 and sm100 only, but SO DOES the image this lane
# served before it, byte for byte (15301320 and 14711496). GB10 loads the sm100
# cubin either way.
# Measured on the box, same flags, same probes, 2026-09-17: greedy median 71.4
# against 69.8 tok/s, acceptance 4.29 against 4.09, conc-check 40/40 serial and
# 160/160 at concurrency 8 on both, needle retrieval exact at 300,108 prompt
# tokens on both (514 s against 493 s, the one point where the release is
# behind). The KV pool needs the fraction moved from 0.70 to 0.76 to match, for
# the reason written at CONTEXT_MODE below.
IMAGE="${IMAGE:-lmsysorg/sglang@sha256:d6e7288627be8b02be88e4bba38e73f6d50e2826869f753c13a4c4385ab3eda9}"  # = lmsysorg/sglang:v0.5.19, 2026-09-04
# Target model choice: "stock" (validated censored base, default) or "uncensored"
# (huihui-ai abliteration re-quantized with the identical RadixArk modelopt
# NVFP4 recipe: same architecture, chat template, MTP + vision, ~22 GB).
STOCK_REPO="RadixArk/Qwen3.8-27B-NVFP4"
STOCK_REV="52d1adc5f38aa5ebf099c29ed7025ba34cfbb854"
# Qwen's own FP8 release: the quality reference of the 27B lane. Weights are 30.9 GB
# against 21 GB for NVFP4, which SGLang takes out of the KV pool: measured on the
# reference box, the same 1m unit reports 863,398 tokens on NVFP4 and 771,139 on
# FP8, so about 92,000 fewer (an earlier note here extrapolated 200,000 from
# 20,000 tokens per GB; the measurement is the number to trust).
# Decode is bandwidth bound on GB10, so it is also slower. No flag changes: SGLang reads
# the scheme from the checkpoint's own config.
FP8_REPO="Qwen/Qwen3.8-27B-FP8"
FP8_REV="017b9c7af6b5689d5dd426a76e0bc077eb5ca20a"
# The abliterated weights in Qwen's own FP8 format: same packager as the NVFP4
# uncensored target above, same fp8 e4m3 scheme as Qwen's official release (which this
# repo already serves), and lm_head is left out of the quantization, which is what keeps
# an FP8 checkpoint loadable by SGLang. Verified against the official FP8: 66 weight
# files, not one hash in common, so the abliteration is real and not a rename.
UNCFP8_REPO="edp1096/Huihui-Qwen3.8-27B-abliterated-FP8"
UNCFP8_REV="603028a116e50e57baeda318eff6181be4ce5876"
UNC_REPO="edp1096/Huihui-RadixArk-Qwen3.8-27B-abliterated-NVFP4"
UNC_REV="21565d389fe573a32c1c425e0c7ade204ddb2263"
# Third target: Qwen3.8-Flash-Next (176B hybrid MoE, 6B active) in NVFP4 on
# SGLang (same engine as the 27B pair), single box. The official image cannot
# fit it on a GB10 as shipped; the locally built two-file overlay mmaps the 51B
# N-gram (PLE) table from NVMe and unblocks QSA decode on sm_121 (see
# flash-sglang/ATTRIBUTION.md). Same service surface: port, API key, keepalive
# proxy, opencode wiring, and (v1.5) working prefix caching, vision and the
# Anthropic endpoint. Serving flags validated on the reference box 2026-08-28.
FLASH_REPO="RadixArk/Qwen3.8-Flash-Next-NVFP4"
FLASH_REV="7b719225242aacd3dbd3f9407468c2ee9a9d2594"
# Fifth checkpoint of the flash lane: NVIDIA's own ModelOpt MIXED_PRECISION
# export of the same model (NVFP4 routed experts, FP8 N-gram table, FP8
# block-scaled MTP experts). On one box its smaller fp8 draft leaves a much
# larger KV pool than the RadixArk export at the same pins (upstream measured
# 174k tokens against 93k with MTP). It needs the mixed-precision loader of
# sglang#38121, which the image below has and the older qwen38flashnext tag
# does not, and it must NOT be passed --quantization (it resolves to
# modelopt_mixed on its own) while --moe-runner-backend has to be pinned to
# flashinfer_cutlass, because the mixed-precision auto-default picks
# flashinfer_trtllm on GB10 and the NVFP4 MoE method rejects it at autotune.
FLASH_NVDA_REPO="nvidia/Qwen3.8-Flash-Next-NVFP4"
FLASH_NVDA_REV="fc694b54fb0174e0913e6adf86691ef85a4ead47"
# Sixth checkpoint of the flash lane: the abliterated build of the same model, in
# the same tree. Chosen on evidence rather than on popularity: of the abliterated
# Flash-Next exports published for this architecture, this one has 206 shards
# with the same names as RadixArk's and 205 of them byte-identical in size, plus
# the same index, the same hf_quant_config and the same chat template, so it is
# an abliteration OF the export this lane already serves and every serving flag
# transfers unchanged. It declares library_name sglang, keeps the MTP head and
# stays multimodal. The alternative with more downloads
# (orcarouter/Qwen3.8-Flash-Next-Uncensored-NVFP4) was rejected here: 18 shards,
# 170.9 GiB, no hf_quant_config and a separate model-mtp.safetensors, so a
# different packaging and a different loader path, none of it validated on this
# recipe. The Mia/Keys splice was rejected for a harder reason: it is built on
# the vLLM tree (architecture Qwen3_8FlashNextForConditionalGeneration,
# model_type qwen3_8_flash_next) and SGLang registers only
# Qwen4ExpForConditionalGeneration, with zero mentions of the other name
# anywhere in the image, so it cannot be served here at all.
FLASH_UNC_REPO="dealignai/Qwen3.8-Flash-Next-ABLITERATED-NVFP4"
FLASH_UNC_REV="be794b990578ef3031eccf9f28e675a289a09ee9"
# The flash lane's image. Since v1.8 this is the image the cookbook points DGX
# Spark at: the qwen4-main-squashed build, which carries the file-backed PLE
# table backend (sglang#37068, replacing this repo's mmap overlay), the merged
# KDA QSA sm_121 decode kernel (sglang#36845, replacing the vendored copy), the
# router fix for the GB10 MTP output collapse (sglang#36811 via #38308/#38290,
# which is the root cause of the wall of "!" the proxy learned to detect in
# v1.6), and the mixed-precision loader (sglang#38121). OVERLAY_FLASH=1 rebuilds
# the old locally-patched image instead.
FLASH_IMAGE="${FLASH_IMAGE:-lmsysorg/sglang@sha256:9d2a843c706c74bc259c0d9abf360551eb2734e1e7d255ab012a6965f10480b6}"  # = lmsysorg/sglang:dev-qwen38-next-local (qwen4-main-squashed 4ccff141db), 2026-09-07
# The base the flash overlay's files were verified against, kept so the rollback
# still builds: OVERLAY_FLASH=1 must graft them onto THAT image, not onto the one
# above, whose module layout they were never diffed against.
OVERLAY_FLASH_BASE_IMAGE="lmsysorg/sglang@sha256:12d3392bdc8be8d35e9a95f191df6aef99c5114bdbefd41bfdc7e760e6d25ec1"  # = lmsysorg/sglang:qwen38flashnext, 2026-08-26
# Backing store for the flash target's file-backed 47.7 GiB PLE table. The
# server rewrites it on every boot (~10 min from a fresh sparse file, ~55 min
# over a populated one), so the launcher deletes the previous file first.
PLE_DIR="${PLE_DIR:-$HOME/flashnext-ple}"
# Resident-set budget for the table's mapping (SGLANG_QWEN4_PLE_FILE_RSS_BUDGET_GB,
# upstream default 8 GiB, 0 disables). A row fault maps in a whole page-cache
# folio, so the mapping's RSS climbs towards 47.7 GiB while a token reads a few
# KB of it; on unified memory that is the same pool the KV cache is sized from,
# which is why upstream trims it. This is the knob behind the pool lottery this
# repo pinned --max-total-tokens for in v1.6.2.
PLE_RSS_BUDGET_GB="${PLE_RSS_BUDGET_GB:-8}"
MODEL_CHOICE="${MODEL_CHOICE:-stock}"
case "$MODEL_CHOICE" in
  stock)      MODEL_REPO="$STOCK_REPO"; MODEL_REV="${MODEL_REV:-$STOCK_REV}" ;;
  uncensored) MODEL_REPO="$UNC_REPO";   MODEL_REV="${MODEL_REV:-$UNC_REV}" ;;
  fp8)        MODEL_REPO="$FP8_REPO";   MODEL_REV="${MODEL_REV:-$FP8_REV}" ;;
  uncensored-fp8) MODEL_REPO="$UNCFP8_REPO"; MODEL_REV="${MODEL_REV:-$UNCFP8_REV}" ;;
  flash)      MODEL_REPO="$FLASH_REPO"; MODEL_REV="${MODEL_REV:-$FLASH_REV}" ;;
  flash-nvda) MODEL_REPO="$FLASH_NVDA_REPO"; MODEL_REV="${MODEL_REV:-$FLASH_NVDA_REV}" ;;
  flash-uncensored) MODEL_REPO="$FLASH_UNC_REPO"; MODEL_REV="${MODEL_REV:-$FLASH_UNC_REV}" ;;
  *) printf 'ERROR: MODEL_CHOICE must be "stock", "uncensored", "fp8", "uncensored-fp8", "flash", "flash-nvda" or "flash-uncensored" (got: %s)\n' "$MODEL_CHOICE" >&2; exit 1 ;;
esac
# Which flags the flash lane's checkpoint wants. The RadixArk export is plain
# NVFP4 and says so; NVIDIA's is a mixed-precision export that resolves its own
# scheme and needs the MoE runner pinned (see FLASH_NVDA_REPO above).
# Both the target's scheme and the DRAFT's belong to the checkpoint, not to the
# serving tier. RadixArk and its abliteration quantized only the routed experts,
# so their 31 in-checkpoint MTP tensors are BF16 and the draft must be told not
# to quantize them; NVIDIA's export ships FP8 block-scaled MTP experts and its
# verified cell passes no draft quantization at all, so forcing "unquant" there
# would load an fp8 head as if it were dense.
#
# A FUNCTION, and called again after the convergence block below, because that
# block can change MODEL_CHOICE (a plain ./install.sh on a flash box converges
# from the default "stock" to "flash"). Computed once, it kept the empty value
# the default had chosen and the rendered launcher lost --quantization
# modelopt_fp4 entirely: the engine then resolved its MoE runner to
# flashinfer_trtllm and died ten minutes into the boot with "Unsupported
# moe_runner_backend for NVFP4 MoE" (2026-09-08, on the reference box).
resolve_flash_checkpoint_args() {
  case "$MODEL_CHOICE" in
    flash|flash-uncensored)
      FLASH_QUANT_ARGS="--quantization modelopt_fp4 --speculative-draft-model-quantization unquant " ;;
    flash-nvda)
      FLASH_QUANT_ARGS="--moe-runner-backend flashinfer_cutlass " ;;
    *)
      FLASH_QUANT_ARGS="" ;;
  esac
}
resolve_flash_checkpoint_args
# Lane implied by the target model: the 27B pair and Flash-Next each have
# their own unit and serving image (both SGLang since v1.5), same port,
# never enabled together.
LANE=27b; UNIT_NAME="qwen38-sglang.service"
case "$MODEL_CHOICE" in
  flash|flash-nvda|flash-uncensored) LANE=flash; UNIT_NAME="qwen38-flash.service" ;;
esac
# Serving tier of the flash lane. Every mamba state slot costs 0.206 GiB of the
# same pool the KV cache comes out of (measured here), and the hybrid GDN/QSA
# model reserves 5 slots per running request with extra_buffer, 4 with
# extra_buffer_lazy, so concurrency and the longest servable prompt trade against
# each other one for one:
#   context      4 requests, 20 slots, MTP: KV pool 295,936 tokens, so a full
#                262,144-token prompt fits. The default, because this lane exists
#                for long context and an agent client runs one or two streams.
#   concurrency  8 requests, 40 slots, MTP: the cookbook's low-latency cell.
#                Measured here: pool 129,792 tokens (prompts stop near 119k),
#                96.5 tok/s aggregate on prose at 8 streams, single stream
#                unchanged. Upstream: 71.7 tok/s at 8, GSM8K 97.1% full set.
#   throughput   24 requests, 96 lazy slots, no speculation: the cookbook's
#                high-throughput cell, 83 tok/s of output at 24 upstream and a
#                ~286k pool, at 15.9 tok/s single stream.
FLASH_TIER="${FLASH_TIER:-context}"
# MTP verify intermediates on a fixed ring instead of per-request state slots
# (the cookbook's EAGLE row on SM120/SM121). Measured on this box 2026-09-12,
# context tier, same target: pool 463,936 -> 557,312 (+20%, above the previous
# boot range), decode neutral (42.4 vs 43.1 greedy median, mixed per-probe
# directions inside the documented uptime drift), canaries 4/4, no corruption.
# Refused with the throughput tier, which runs no speculation. On unless
# asked: measured +20% pool on this box, decode neutral, canaries 4/4.
FLASH_REPLAYSSM_SPEC="${FLASH_REPLAYSSM_SPEC:-1}"
# Also a function, and for the same reason: the convergence block can adopt the
# tier the installed launcher is already serving.
resolve_flash_tier_args() {
  case "$FLASH_TIER" in
    context)
      FLASH_TIER_ARGS="--max-running-requests 4 --max-mamba-cache-size 20 --mamba-radix-cache-strategy extra_buffer --speculative-algorithm NEXTN --speculative-num-steps 3 --speculative-eagle-topk 1 --speculative-num-draft-tokens 4" ;;
    concurrency)
      FLASH_TIER_ARGS="--max-running-requests 8 --max-mamba-cache-size 40 --mamba-radix-cache-strategy extra_buffer --speculative-algorithm NEXTN --speculative-num-steps 3 --speculative-eagle-topk 1 --speculative-num-draft-tokens 4" ;;
    throughput)
      FLASH_TIER_ARGS="--max-running-requests 24 --max-mamba-cache-size 96 --mamba-radix-cache-strategy extra_buffer_lazy" ;;
    *) printf 'ERROR: FLASH_TIER must be "context", "concurrency" or "throughput" (got: %s)\n' "$FLASH_TIER" >&2; exit 1 ;;
  esac
  case "$FLASH_REPLAYSSM_SPEC" in
    0|"") ;;
    1)
      # No speculation on throughput, so nowhere to apply it: say so and skip,
      # instead of failing a convergent throughput reinstall on the new default.
      case "$FLASH_TIER" in
        throughput) echo "NOTE: FLASH_REPLAYSSM_SPEC=1 has no speculation to apply to on the throughput tier; ignored" ;;
        *) FLASH_TIER_ARGS="$FLASH_TIER_ARGS --enable-linear-replayssm-spec" ;;
      esac ;;
    *) printf 'ERROR: FLASH_REPLAYSSM_SPEC must be 0 or 1 (got: %s)\n' "$FLASH_REPLAYSSM_SPEC" >&2; return 1 ;;
  esac
}
resolve_flash_tier_args
# The cookbook's verified value for both single-Spark cells. Lower it if your box
# runs co-tenants: the pools are sized from what the host has free at profiling.
FLASH_MEM_FRACTION="${FLASH_MEM_FRACTION:-0.85}"
# Reduced draft vocabulary, in token ids (0 = off). A speculative step's draft
# reads the model's lm_head in full, and at 248,320 x 2560 in BF16 that is
# 1.18 GiB read three times per MTP-3 engine step. SGLang can hand the draft a
# sliced head instead (--speculative-token-map): the target still verifies over
# the whole vocabulary, so this cannot change what the model is allowed to say.
# build-token-map.py writes the list; install.sh builds it inside the serving
# image, where the tokenizer version matches the served model by construction.
# The size is a measured choice, not a guess: see BENCHMARKS.md.
SPEC_TOKEN_MAP_SIZE="${SPEC_TOKEN_MAP_SIZE:-65536}"
# The file name carries the size, so a box that changes SPEC_TOKEN_MAP_SIZE
# builds a new map instead of serving the old one under a new intent. Defined
# here and not earlier on purpose: the first version of this line sat above the
# size it interpolates, and ${...:-0} turned that ordering bug into a silently
# mis-named file rather than an error.
TOKEN_MAP_NAME="token-map-${SPEC_TOKEN_MAP_SIZE}.pt"
# The DSpark drafter. Served up to v1.1; DFlash2 replaced it in v1.2 and no
# serving path has referenced it since, so it is no longer downloaded (it cost
# 6 GB and a few minutes on every 27B install). The pin stays so the cockpit's
# registry can still recognise a copy left on disk, and `git checkout v1.1 &&
# ./install.sh` fetches it through that release's own installer.
# RadixArk published a v2 of this draft on 2026-08-28 claiming +26% acceptance
# over v1 (3.43 aggregate over 64,675 prompts on their own card), which would
# put it level with DFlash2 on paper. Measured here on 2026-09-18, same box,
# same unit, only the drafter swapped (b9a5dbdf, DSPARK, gamma auto-inferred):
# greedy median 48.7 tok/s against DFlash2's 72.0, acceptance 3.49 against 4.29,
# conc-check clean on both. It does leave a larger pool (953,442 against
# 889,131) because its verify window is 8 rather than 16, and that does not come
# close to paying for a third of the throughput. The lane stays on DFlash2.
# shellcheck disable=SC2034  # read externally: dashboard/registry.py pairs DRAFT_REV
# with this repo id to classify a leftover DSpark copy, and uninstall.sh lists it.
DRAFT_REPO="RadixArk/Qwen3.8-27B-DSpark"
DRAFT_REV="${DRAFT_REV:-85ef153be924f17ce4bf62726954eeaa4a73e854}"
# The DFlash2 drafter the 27B lane serves. Since v1.9 this is maurienne-ai's
# calibrated NVFP4 build of the z-lab draft (3.53 GB BF16 -> 1.37 GB of VRAM,
# same acceptance once calibrated), served 16 tokens deep instead of 8.
# Measured on the reference box, same target, same flags otherwise:
# bench.sh greedy median 50 -> 65.3 tok/s (+30%), bench-matrix code 41 -> 40,
# tech-FR 26 -> 32, reasoning-FR 43.5 -> 49.5, prose unchanged, pool 357,706
# tokens, 0 corruption markers, 4 concurrent streams clean. The drafters are
# lossless by construction (the target verifies every drafted token), and the
# depth was swept, not guessed (D8 -> D16 alone is +19% pooled throughput).
# Rollback to the BF16 draft: DRAFT2_REPO=z-lab/Qwen3.8-27B-DFlash2
# DRAFT2_REV=50307d4c4cde6860d4eee73e2547cd786fe8e8a4 DRAFT2_QUANT=unquant
# DRAFT2_TOKENS=8 ./install.sh (then restart the unit).
DRAFT2_REPO="${DRAFT2_REPO:-maurienne-ai/Qwen3.8-27B-DFlash2-NVFP4-RTNcal}"
DRAFT2_REV="${DRAFT2_REV:-bd7a934213c47a9e7ef69eef36bb3325f47fd1f1}"
DRAFT2_QUANT="${DRAFT2_QUANT:-modelopt_fp4}"
DRAFT2_TOKENS="${DRAFT2_TOKENS:-16}"
# Context mode: "1m" (1,010,000 via YaRN static scaling, mem-fraction 0.76,
# plus the keepalive proxy for agent clients) or "native" (262144). Since
# v1.12.1 an unset CONTEXT_MODE means 1m on the 27B lane: it is the preset the
# reference box has served daily since 2026-08-22, and a stack whose headline
# window has to be asked for by name ships it off for almost everybody.
# Empty here means "not chosen yet". The default is resolved further down,
# once the lane, the flags and the installed unit are all known, because the
# two paths that cannot serve 1m (the flash lane and --no-service) have to
# fall back to native in silence rather than refuse a default nobody typed.
# The fraction is 0.76 since v1.14, and the number is the image's, not a taste.
# The official image sizes its static budget more conservatively than the
# locally built one this lane served before: at an identical 0.70 it came up
# with a 770,118-token pool against 906,524, while leaving 29.89 GB of GPU
# memory unused against 19.38. It was never short of memory, it just did not
# claim it. 0.76 hands the pool back (902,398 measured, inside the boot-to-boot
# spread this box shows on one image: 906,524 then 910,203) and still leaves a
# wider margin than the old pin did, 21.30 GB against 19.38.
# What bounds this number is the unified pool, NOT the container: --memory 100g
# is a host-RSS cap and the cgroup does not see CUDA unified allocations.
# Measured 2026-09-18 at 0.76 under a 5,623-token generation, the container held
# 7.15 GiB of its 100 GiB the whole way. Read the GPU-side headroom instead
# (available_gpu_mem after graph capture), which is what every number here is.
CONTEXT_MODE="${CONTEXT_MODE:-}"
case "$CONTEXT_MODE" in
  native|1m|"") ;;
  *) printf 'ERROR: CONTEXT_MODE must be "native" or "1m" (got: %s)\n' "$CONTEXT_MODE" >&2; exit 1 ;;
esac
if [ "$LANE" = "flash" ] && [ "$CONTEXT_MODE" = "1m" ]; then
  printf 'ERROR: CONTEXT_MODE=1m is a 27B mode. Flash-Next serves its full native 262144 window\n' >&2
  printf '       by default; a validated long-context mode for it may come in a later release.\n' >&2
  exit 1
fi
# What actually gets served. Until v1.7 both lanes served a locally built image:
# the pinned official base plus sha256-verified overlay files, because no
# official image carried DFLASH2 or ran Flash-Next on one GB10. Both are
# upstream now, so both lanes serve the pinned official image directly and
# build nothing. The flash lane crossed over in v1.8; the 27B lane followed in
# v1.14, once the two things holding it back were measured rather than assumed
# (see the IMAGE pin above). OVERLAY_FLASH=1 rebuilds the flash lane's old
# locally-patched image, which is the only overlay this repo still carries.
OVERLAY_FLASH="${OVERLAY_FLASH:-${OVERLAY:-0}}"
# The local tag of the flash overlay path. Every line below sits at column 0 and
# refers only to names defined above it, because run.sh and switch-model.sh read
# these assignments out of this file and eval them with nothing else bound.
OVERLAY_FLASH_SERVE_IMAGE="qwen38-flash:v1.6.0-kda"
SERVE_IMAGE="${SERVE_IMAGE:-$IMAGE}"
FLASH_SERVE_IMAGE="${FLASH_SERVE_IMAGE:-$FLASH_IMAGE}"
# The non-default overlay choice, applied only when the operator did not name a
# serving image outright.
if [ -z "$_ENV_FLASH_SERVE_IMAGE" ] && [ "$OVERLAY_FLASH" = "1" ]; then
  FLASH_SERVE_IMAGE="$OVERLAY_FLASH_SERVE_IMAGE"
fi
# opencode, the client this repo configures and serves in the cockpit's Agent tab, is
# pinned like everything else. Four things the repo does were read out of this
# version's own binary and checked against its behaviour: the compaction threshold
# oc-fit-limits.py sizes the limits for, the hidden 32,000-token output cap the oc
# launcher lifts (182,000 sent with the variable, 32,000 without, measured on a fake
# endpoint), the overflow phrases the proxy answers with so a refusal triggers a
# compaction (21 of 21 present), and --yolo / OPENCODE_PERMISSION for the Agent tab.
# 1.18.32 passed all of it against the 1.18.27 the repo was measured on (2026-09-23).
# Left alone, opencode installs its own patch releases, so two boxes installed a
# week apart run two versions. The sha256 is GitHub's own digest of the release asset
# opencode-linux-arm64.tar.gz. OPENCODE_PIN=0 keeps whatever opencode you have.
_ENV_OPENCODE_VERSION="${OPENCODE_VERSION:-}"
_ENV_OPENCODE_SHA256="${OPENCODE_SHA256:-}"
OPENCODE_VERSION="${OPENCODE_VERSION:-1.18.32}"
OPENCODE_SHA256="${OPENCODE_SHA256:-568461b7d4d8c19865c97e9a1102e613049c6039d01fe772154de873c1865840}"
OPENCODE_PIN="${OPENCODE_PIN:-1}"
PORT="${PORT:-30000}"
HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"
CONFIG_DIR="$HOME/.config/qwen38"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NO_START=0
NO_SERVICE=0
NO_OPENCODE=0
WITH_OPENCODE=0
NO_COCKPIT=0
WITH_COCKPIT=0
WITH_IMAGE=0
NO_IMAGE=0
for arg in "$@"; do
  case "$arg" in
    --no-start) NO_START=1 ;;
    --no-service) NO_SERVICE=1 ;;
    --no-opencode) NO_OPENCODE=1 ;;
    --with-opencode) WITH_OPENCODE=1 ;;
    --no-cockpit) NO_COCKPIT=1 ;;
    --with-cockpit) WITH_COCKPIT=1 ;;
    --with-image) WITH_IMAGE=1 ;;
    --no-image) NO_IMAGE=1 ;;
    --with-claude-warmup)
      echo "NOTE: --with-claude-warmup was removed in v1.3 (the repo's client story moved to opencode)."
      echo "      The flag is ignored; an installed warmup drop-in from an earlier version is cleaned up." ;;
    -h|--help)
      cat <<'HLP'
Usage: ./install.sh [--no-start] [--no-service] [--no-opencode | --with-opencode]
                   [--no-cockpit | --with-cockpit] [--with-image | --no-image]

Run it as yourself. Never with sudo in front: it calls sudo itself for the
privileged steps, and under sudo every path it writes moves to /root (see the
refusal at the top of this file).

A plain run installs the whole box: engine, keepalive proxy, opencode wiring,
the cockpit and its Agent tab. When it finishes it prints the cockpit URL, and
from there you start, stop, switch, watch and benchmark without a terminal.

  --no-start            install everything but don't start the service now
  --no-service          no systemd, no sudo: just prepare everything (image,
                        checkpoints, key, template), then run in the foreground
                        anytime with ./run.sh (Ctrl+C stops it)
  --no-opencode         skip the opencode integration (no generated config, no
                        oc launcher, and switch-model.sh leaves your opencode
                        default model alone). Remembered by later runs.
  --with-opencode       re-enable it after a --no-opencode
  --no-cockpit          skip the web cockpit and its Agent tab (no dashboard
                        unit, no sudoers allowlist). Remembered by later runs.
  --with-cockpit        re-enable it after a --no-cockpit
  --with-image          also install the Qwen-Image 2.1 lane: text-to-image,
                        image editing and native RGBA, served on its own port
                        and driven from the cockpit's Image tab. Adds 38 GB
                        (31 checkpoint, 7 runtime) and about 25 min, which is
                        why a plain run does not. Once installed, later runs
                        keep and update it.
  --no-image            skip it on a box that already has it (the unit and the
                        venv stay in place; ./install-image.sh --uninstall
                        removes them)

Re-running over an existing install keeps the operator's choices: the target
model (any of the seven), the context mode (native/1m), the flash serving tier,
the port, the HF cache location and the opencode on/off choice are read from
the installed unit or launcher (or the marker file) unless the env var or flag
is passed explicitly.

Env overrides (defaults are pinned to the versions validated 2026-09-11):
  IMAGE=lmsysorg/sglang:v0.5.19      the moving tag of the pinned digest, instead
                                     of the digest itself
  MODEL_REV=main                     use the latest target revision
  DRAFT2_REV=main                    latest DFlash2 draft revision
  DRAFT2_REPO=z-lab/Qwen3.8-27B-DFlash2 DRAFT2_REV=50307d4c4cde6860d4eee73e2547cd786fe8e8a4 DRAFT2_QUANT=unquant DRAFT2_TOKENS=8
                                     roll the 27B lane back to the BF16 draft
                                     at depth 8 (the default since v1.9 is the
                                     calibrated NVFP4 draft at depth 16)
  MODEL_CHOICE=uncensored            serve the huihui-abliterated model
                                     (edp1096 NVFP4) instead of the stock base
  MODEL_CHOICE=fp8                   serve Qwen's own FP8 release (30.9 GB, about
                                     200k fewer tokens of KV pool, slower decode)
  MODEL_CHOICE=uncensored-fp8        the huihui abliteration in that same FP8 format
  MODEL_CHOICE=flash                 serve Qwen3.8-Flash-Next (176B hybrid MoE,
                                     NVFP4, SGLang engine, ~126 GB download; the
                                     47.7 GiB N-gram table is served from a file
                                     on NVMe, with prefix caching and vision)
  MODEL_CHOICE=flash-uncensored      the abliterated build of the same tree
                                     (~126 GB): 205 of its 206 shards are
                                     byte-identical in size to the stock export,
                                     so every serving flag is the same one
  MODEL_CHOICE=flash-nvda            the same model from NVIDIA's own mixed-
                                     precision export (~124 GB download), served
                                     with no --quantization and a pinned MoE
                                     runner; measured here as a tie with stock
  SPEC_TOKEN_MAP_SIZE=0              flash: serve without the reduced draft
                                     vocabulary (it is worth 14 to 25% of decode
                                     and cannot change what the model may say)
  FLASH_TIER=concurrency             flash only: 8 concurrent requests with the
                                     MTP head instead of 4, at a third of the KV
                                     pool; "throughput" is 24 without speculation
  FLASH_REPLAYSSM_SPEC=0            flash only: keep MTP verify intermediates
                                     in per-request state slots instead of the
                                     fixed ring (measured +20% pool on this box,
                                     decode neutral, canaries 4/4)
  FLASH_MEM_FRACTION=0.85            flash only: static memory fraction
  PLE_RSS_BUDGET_GB=8                flash only: resident-set budget of the
                                     N-gram table's mapping (0 disables the trim)
  PLE_DIR=~/flashnext-ple            flash only: where the 47.7 GiB backing file
                                     of the N-gram table lives
  CONTEXT_MODE=native                the 262144 window instead of the 1,010,000
                                     one. Since v1.12.1 a 27B install serves 1m
                                     by default (YaRN static scaling, README,
                                     "The 1M context mode"); the flash lane and
                                     --no-service are native either way, and a
                                     re-run keeps whatever is already installed
  PROXY_PORT=30001                   keepalive proxy port (default: PORT+1)
  OVERLAY_FLASH=1                    flash: serve the locally built overlay
                                     image of v1.7 instead of the official one
  SERVE_IMAGE=ref                    serving image for the 27B lane
  FLASH_SERVE_IMAGE=ref              serving image for the Flash-Next lane
  HF_CACHE=/path                     HuggingFace cache location (~28 GB for a 27B
                                     target, ~126 GB for flash)
  PORT=30000                         serving port
HLP
      exit 0 ;;
    *) printf 'Unknown flag: %s (see --help)\n' "$arg" >&2; exit 1 ;;
  esac
done

# ── Converge on the operator's installed choices ──
# Re-running the installer (or the get.sh one-liner) must never silently reset
# a choice that is already serving: the target model (any of the seven targets),
# the port, and the HF cache location are read from the installed units and
# kept, unless the corresponding env var was passed explicitly on this
# invocation. Everything else (image digest, checkpoint revisions, launch
# flags) always follows the repo: that is what an upgrade is.
KEEP_MODEL_VERBATIM=0
SGL_UNIT_PATH="/etc/systemd/system/qwen38-sglang.service"
FLASH_UNIT_PATH="/etc/systemd/system/qwen38-flash.service"
for _p in "$SGL_UNIT_PATH" "$FLASH_UNIT_PATH"; do
  if [ -f "$_p" ] && [ ! -r "$_p" ]; then
    echo "NOTE: an installed unit ($_p) exists but is not readable (pre-v1.2.3 installs used mode 600),"
    echo "      so its choices cannot be preserved automatically. If you had a custom PORT= or"
    echo "      MODEL_CHOICE=, pass them explicitly on this command."
  fi
done
# Which interface the engine and the proxy answer on.
#
# THE ENGINE IS LOCALHOST-ONLY SINCE v1.17, and that is a changed default. It listened on
# every interface before, and the measurement that decided it: seven days of journal on
# the reference box, 581,479 requests to the engine, every single one from 127.0.0.1,
# while the machine was reached from a laptop and a phone on the cockpit port alone. An
# open port nobody uses would be an ordinary waste; this one is the port SGLang dies on
# rather than refuses (sglang#40076 and sglang#31597, both unfixed upstream, both refused
# at the proxy since v1.15.0). Leaving it open leaves a door beside the one with the lock.
#
# Nothing is lost by closing it, which is why the default could move at all: the proxy on
# PORT+1 is a full pass-through, every route the engine serves and the same OpenAI and
# Anthropic dialects, plus the guards. A client that pointed at :30000 from another
# machine points at :30001 and gets more, not less. ENGINE_BIND=0.0.0.0 restores the old
# behaviour for a box that wants it.
#
# The proxy's own default does NOT move: it is the door clients are told to use, the
# README has said so since v1.5, and it is the one that refuses what the engine cannot.
ENGINE_BIND="${ENGINE_BIND:-127.0.0.1}"
PROXY_BIND="${PROXY_BIND:-0.0.0.0}"
for _b in "$ENGINE_BIND" "$PROXY_BIND"; do
  case "$_b" in
    *[!0-9.]*|"") printf 'ERROR: ENGINE_BIND and PROXY_BIND take an IPv4 address (got: %s)\n' "$_b" >&2; exit 1 ;;
  esac
done

# Which choice is installed? The flash unit wins only when it is the enabled
# one; a box can hold both unit files but only one engine serves the port.
# On the (hand-made) both-enabled state, the 27B unit is followed and a note
# is printed: predictable beats clever here.
INSTALLED_CHOICE=""
SGL_READABLE=0; FLASH_READABLE=0
[ -f "$SGL_UNIT_PATH" ] && [ -r "$SGL_UNIT_PATH" ] && SGL_READABLE=1
[ -f "$FLASH_UNIT_PATH" ] && [ -r "$FLASH_UNIT_PATH" ] && FLASH_READABLE=1
FLASH_ENABLED=0; SGL_ENABLED=0
systemctl is-enabled --quiet qwen38-flash.service 2>/dev/null && FLASH_ENABLED=1
systemctl is-enabled --quiet qwen38-sglang.service 2>/dev/null && SGL_ENABLED=1
# The image lane is a lane too, and when it is the one enabled at boot it was switched to
# on purpose. A plain re-run must keep that, the same promise it keeps for the target, the
# context mode and the port: without this it fell through to "27b" below, enabled the 27B
# unit beside the image one, and left two engines set to start at the next reboot, with
# only the order systemd happened to pull them in deciding which one came up.
IMAGE_BOOT=0
if systemctl is-enabled --quiet qwen38-image.service 2>/dev/null \
   && [ "$SGL_ENABLED" -eq 0 ] && [ "$FLASH_ENABLED" -eq 0 ]; then
  if [ -n "$_ENV_MODEL_CHOICE" ]; then
    # An explicit MODEL_CHOICE asks for a text lane: honour it, and the image lane leaves
    # the boot below, where the text unit is enabled. Keeping the image lane here would
    # answer "install flash" with "flash updated, not served", and exit 0.
    echo "MODEL_CHOICE=$_ENV_MODEL_CHOICE given on a box booting the image lane: making that text lane the boot lane again."
  else
    IMAGE_BOOT=1
    echo "The image lane is this box's boot lane: this run brings the text lane up to date"
    echo "without making it the boot lane again. Switch back to text from the cockpit when you want it."
  fi
fi
# Which text lane to bring up to date on that path. Both text units can be on disk with
# neither enabled, since a switch to images disables both, so enablement cannot say which
# one was in use; the switch writes it down instead, and without it the 27B lane is the
# fallback, the same one the code below picks when nothing is enabled.
LANE_BEFORE_IMAGE="$(cat "$CONFIG_DIR/lane-before-image" 2>/dev/null || true)"
if [ "$FLASH_READABLE" -eq 1 ] && [ "$FLASH_ENABLED" -eq 1 ] && [ "$SGL_ENABLED" -eq 1 ]; then
  echo "NOTE: both qwen38-sglang and qwen38-flash are enabled (only one can serve the port)."
  echo "      Following the 27B unit; run ./switch-model.sh to resolve this cleanly."
fi
if [ "$FLASH_READABLE" -eq 1 ] && [ "$FLASH_ENABLED" -eq 1 ] && [ "$SGL_ENABLED" -eq 0 ]; then
  INSTALLED_CHOICE=flash
elif [ "$IMAGE_BOOT" -eq 1 ] && [ "$LANE_BEFORE_IMAGE" = "qwen38-flash.service" ] && [ "$FLASH_READABLE" -eq 1 ]; then
  INSTALLED_CHOICE=flash          # the text lane this box used before it switched to images
elif [ "$SGL_READABLE" -eq 1 ]; then
  INSTALLED_CHOICE=27b
elif [ "$FLASH_READABLE" -eq 1 ]; then
  # flash unit present but not enabled and no sglang unit: still the only choice
  INSTALLED_CHOICE=flash
fi
if [ "$INSTALLED_CHOICE" = "flash" ]; then
  # Converge on the installed flash launch script (the unit only points at it;
  # the engine flags and mounts live in $CONFIG_DIR/launch-flash.sh).
  FLASH_LAUNCH="$CONFIG_DIR/launch-flash.sh"
  CUR_REV=""; CUR_PORT=""; CUR_HF=""
  if [ -r "$FLASH_LAUNCH" ]; then
    CUR_REV="$(grep -oE -- '--revision [^ ]+' "$FLASH_LAUNCH" | head -1 | cut -d' ' -f2 || true)"
    if grep -q 'sglang.launch_server' "$FLASH_LAUNCH"; then
      # v1.5+ shape (SGLang, host networking): --port IS the host port.
      CUR_PORT="$(grep -oE -- '--port [0-9]+' "$FLASH_LAUNCH" | head -1 | tr -dc '0-9' || true)"
      CUR_HF="$(grep -oE -- '-v [^ :]+:/root/\.cache/huggingface' "$FLASH_LAUNCH" | head -1 | sed -e 's/^-v //' -e 's|:/root/\.cache/huggingface$||' || true)"
    else
      # v1.4 shape (vLLM, bridge networking): the host port is the -p mapping;
      # its in-container --port 8000 must NOT be read as a host port. The
      # upgrade regenerates the launcher on the new engine, keeping only
      # port/HF/model choices.
      CUR_PORT="$(grep -oE -- '-p [0-9]+:8000' "$FLASH_LAUNCH" | head -1 | grep -oE '^-p [0-9]+' | tr -dc '0-9' || true)"
      CUR_HF="$(grep -oE -- '-v [^ :]+:/hf' "$FLASH_LAUNCH" | head -1 | sed -e 's/^-v //' -e 's|:/hf$||' || true)"
      echo "NOTE: the installed flash lane is the v1.4 vLLM engine; this upgrade moves it to SGLang"
      echo "      (working prefix caching; see CHANGELOG v1.5). Port/cache/model choices are kept."
    fi
    CUR_PLE="$(grep -oE -- '-v [^ :]+:/ple' "$FLASH_LAUNCH" | head -1 | sed -e 's/^-v //' -e 's|:/ple$||' || true)"
    CUR_MODEL="$(grep -oE -- '--model-path [^ ]+' "$FLASH_LAUNCH" | head -1 | cut -d' ' -f2 || true)"
    # The tier lives in the launcher as the concurrency it pins, so a re-run
    # without FLASH_TIER keeps the tier the box is serving.
    if [ -z "${FLASH_TIER_ENV:-}" ]; then
      for t in concurrency throughput; do
        case "$t" in
          concurrency) mrr=8 ;;
          throughput)  mrr=24 ;;
        esac
        if grep -q -- "--max-running-requests $mrr" "$FLASH_LAUNCH" 2>/dev/null; then
          FLASH_TIER="$t"
          echo "Keeping the installed flash tier: $t. Pass FLASH_TIER=context to change."
        fi
      done
      resolve_flash_tier_args
    fi
    if [ -z "${_ENV_PLE_DIR:-}" ] && [ -n "$CUR_PLE" ] && [ "$CUR_PLE" != "$PLE_DIR" ]; then
      PLE_DIR="$CUR_PLE"
      echo "Keeping the installed PLE table location: $PLE_DIR. Pass PLE_DIR= to change."
    fi
  fi
  if [ -z "$_ENV_MODEL_CHOICE" ] && [ "$LANE" != "flash" ]; then
    MODEL_CHOICE=flash; MODEL_REPO="$FLASH_REPO"; MODEL_REV="${_ENV_MODEL_REV:-$FLASH_REV}"
    if [ -n "${CUR_MODEL:-}" ] && [ "$CUR_MODEL" = "$FLASH_NVDA_REPO" ]; then
      MODEL_CHOICE=flash-nvda; MODEL_REPO="$FLASH_NVDA_REPO"; MODEL_REV="${_ENV_MODEL_REV:-$FLASH_NVDA_REV}"
    elif [ -n "${CUR_MODEL:-}" ] && [ "$CUR_MODEL" = "$FLASH_UNC_REPO" ]; then
      MODEL_CHOICE=flash-uncensored; MODEL_REPO="$FLASH_UNC_REPO"; MODEL_REV="${_ENV_MODEL_REV:-$FLASH_UNC_REV}"
    fi
    LANE=flash; UNIT_NAME="qwen38-flash.service"
    echo "Keeping the installed target model: $MODEL_CHOICE ($MODEL_REPO). Pass MODEL_CHOICE= to change."
  fi
elif [ "$INSTALLED_CHOICE" = "27b" ]; then
  UNIT_PATH="$SGL_UNIT_PATH"
  CUR_MODEL="$(grep -oE -- '--model-path [^ ]+' "$UNIT_PATH" | head -1 | cut -d' ' -f2 || true)"
  # The target's --revision (never matches --speculative-draft-model-revision:
  # that flag has a single dash before "revision")
  CUR_REV="$(grep -oE -- '--revision [^ ]+' "$UNIT_PATH" | head -1 | cut -d' ' -f2 || true)"
  CUR_PORT="$(grep -oE -- '--port [0-9]+' "$UNIT_PATH" | head -1 | tr -dc '0-9' || true)"
  CUR_HF="$(grep -oE -- '-v [^ :]+:/root/\.cache/huggingface' "$UNIT_PATH" | head -1 | sed -e 's/^-v //' -e 's|:/root/\.cache/huggingface$||' || true)"
  if [ -z "$_ENV_MODEL_CHOICE" ] && [ -n "$CUR_MODEL" ] && [ "$CUR_MODEL" != "$MODEL_REPO" ]; then
    case "$CUR_MODEL" in
      "$STOCK_REPO")  MODEL_CHOICE=stock;          MODEL_REPO="$STOCK_REPO";  MODEL_REV="${_ENV_MODEL_REV:-$STOCK_REV}" ;;
      "$UNC_REPO")    MODEL_CHOICE=uncensored;     MODEL_REPO="$UNC_REPO";    MODEL_REV="${_ENV_MODEL_REV:-$UNC_REV}" ;;
      "$FP8_REPO")    MODEL_CHOICE=fp8;            MODEL_REPO="$FP8_REPO";    MODEL_REV="${_ENV_MODEL_REV:-$FP8_REV}" ;;
      "$UNCFP8_REPO") MODEL_CHOICE=uncensored-fp8; MODEL_REPO="$UNCFP8_REPO"; MODEL_REV="${_ENV_MODEL_REV:-$UNCFP8_REV}" ;;
      *) KEEP_MODEL_VERBATIM=1; MODEL_CHOICE=custom; MODEL_REPO="$CUR_MODEL" ;;
    esac
    LANE=27b; UNIT_NAME="qwen38-sglang.service"
    if [ "$KEEP_MODEL_VERBATIM" -eq 1 ]; then
      echo "NOTE: keeping the custom --model-path already installed ($CUR_MODEL)."
      echo "      Its download and template steps are skipped (it is already serving from cache)."
      echo "      Pass MODEL_CHOICE=stock, uncensored, fp8 or uncensored-fp8 to override."
    else
      echo "Keeping the installed target model: $MODEL_CHOICE ($MODEL_REPO). Pass MODEL_CHOICE= to change."
    fi
  fi
  # Same promise for the drafter: a box rolled back to the BF16 draft via the
  # env override keeps it across plain re-runs, the way MODEL_CHOICE is kept.
  if [ -z "$_ENV_DRAFT2_REPO$_ENV_DRAFT2_REV$_ENV_DRAFT2_QUANT$_ENV_DRAFT2_TOKENS" ]; then
    CUR_DRAFT="$(grep -oE -- '--speculative-draft-model-path [^ ]+' "$UNIT_PATH" | head -1 | cut -d' ' -f2 || true)"
    CUR_DRAFT_REV="$(grep -oE -- '--speculative-draft-model-revision [^ ]+' "$UNIT_PATH" | head -1 | cut -d' ' -f2 || true)"
    CUR_DRAFT_QUANT="$(grep -oE -- '--speculative-draft-model-quantization [^ ]+' "$UNIT_PATH" | head -1 | cut -d' ' -f2 || true)"
    CUR_DRAFT_TOKENS="$(grep -oE -- '--speculative-num-draft-tokens [0-9]+' "$UNIT_PATH" | head -1 | tr -dc '0-9' || true)"
    if [ -n "$CUR_DRAFT" ] && [ "$CUR_DRAFT" != "$DRAFT2_REPO" ]; then
      DEF_DRAFT2="DRAFT2_REPO=$DRAFT2_REPO DRAFT2_REV=$DRAFT2_REV DRAFT2_QUANT=$DRAFT2_QUANT DRAFT2_TOKENS=$DRAFT2_TOKENS"
      DRAFT2_REPO="$CUR_DRAFT"
      [ -n "$CUR_DRAFT_REV" ] && DRAFT2_REV="$CUR_DRAFT_REV"
      [ -n "$CUR_DRAFT_QUANT" ] && DRAFT2_QUANT="$CUR_DRAFT_QUANT"
      [ -n "$CUR_DRAFT_TOKENS" ] && DRAFT2_TOKENS="$CUR_DRAFT_TOKENS"
      echo "Keeping the installed drafter: $DRAFT2_REPO (D=$DRAFT2_TOKENS, $DRAFT2_QUANT). Pass DRAFT2_REPO= to change."
      if [ "$CUR_DRAFT" = "z-lab/Qwen3.8-27B-DFlash2" ]; then
        # The default of v1.2.3 to v1.8.6, and the documented rollback since: the unit
        # cannot say which, so it is kept, and a box that was only ever updated never
        # got v1.9's draft (found in review, 2026-09-24). Said here, with the way over.
        echo "NOTE: that is the BF16 draft installs used before v1.9. The default since v1.9 drafts from a"
        echo "      calibrated NVFP4 head, measured +30% there on the reference box (lossless). To move to it:"
        echo "      $DEF_DRAFT2 ./install.sh"
      fi
    elif [ -n "$CUR_DRAFT_TOKENS" ] && [ "$CUR_DRAFT_TOKENS" != "$DRAFT2_TOKENS" ]; then
      DRAFT2_TOKENS="$CUR_DRAFT_TOKENS"
      [ -n "$CUR_DRAFT_QUANT" ] && DRAFT2_QUANT="$CUR_DRAFT_QUANT"
      echo "Keeping the installed draft depth: D=$DRAFT2_TOKENS. Pass DRAFT2_TOKENS= to change."
    fi
  fi
fi
if [ -n "$INSTALLED_CHOICE" ]; then
  if [ -z "$_ENV_PORT" ] && [ -n "${CUR_PORT:-}" ] && [ "$CUR_PORT" != "$PORT" ]; then
    PORT="$CUR_PORT"
    echo "Keeping the installed port: :$PORT. Pass PORT= to change."
  fi
  if [ -z "$_ENV_HF_CACHE" ] && [ -n "${CUR_HF:-}" ] && [ "$CUR_HF" != "$HF_CACHE" ]; then
    HF_CACHE="$CUR_HF"
    echo "Keeping the installed HF cache location: $HF_CACHE. Pass HF_CACHE= to change."
  fi
  # The same promise the context mode gets, for the same reason: a box hardened to
  # localhost must not be reopened by a plain update. An installed choice wins over a
  # default, both ways.
  if [ -z "${_ENV_ENGINE_BIND:-}" ]; then
    # Read where the engine's --host really is: the 27B unit, or the flash launcher (its
    # unit only points at the script). This read $UNIT_PATH, which only the 27B branch
    # sets, so every re-run of a flash box printed "UNIT_PATH: unbound variable" and put
    # the engine back on 127.0.0.1 over the box's own choice (found in review, 2026-09-24).
    if [ "$INSTALLED_CHOICE" = "flash" ]; then BIND_FROM="$FLASH_LAUNCH"; else BIND_FROM="$UNIT_PATH"; fi
    CUR_BIND="$(grep -oE -- '--host [0-9.]+' "$BIND_FROM" 2>/dev/null | head -1 | cut -d' ' -f2 || true)"
    if [ -n "$CUR_BIND" ] && [ "$CUR_BIND" != "$ENGINE_BIND" ]; then
      ENGINE_BIND="$CUR_BIND"
      echo "Keeping the installed engine bind: $ENGINE_BIND. Pass ENGINE_BIND= to change."
    fi
  fi
  # The unit name, not $KEEPALIVE_UNIT: that variable is set 700 lines below this block.
  if [ -z "${_ENV_PROXY_BIND:-}" ] && [ -r "/etc/systemd/system/qwen38-keepalive.service" ]; then
    CUR_PBIND="$(grep -oE -- 'PROXY_BIND=[0-9.]+' "/etc/systemd/system/qwen38-keepalive.service" | head -1 | cut -d= -f2 || true)"
    if [ -n "$CUR_PBIND" ] && [ "$CUR_PBIND" != "$PROXY_BIND" ]; then
      PROXY_BIND="$CUR_PBIND"
      echo "Keeping the installed proxy bind: $PROXY_BIND. Pass PROXY_BIND= to change."
    fi
  fi
  if [ "$INSTALLED_CHOICE" = "27b" ] && [ "$LANE" != "flash" ] && [ -z "$_ENV_CONTEXT_MODE" ]; then
    if grep -q -- '--context-length 1010000' "$SGL_UNIT_PATH"; then
      CONTEXT_MODE=1m
      echo "Keeping the installed context mode: 1m. Pass CONTEXT_MODE=native to change."
    elif [ -z "$CONTEXT_MODE" ]; then
      # The other direction, and since v1.12.1 it has to exist: with 1m as the
      # default, a plain re-run on a box installed native would patch YaRN into
      # its cached configs and move its memory fraction, which is not what an
      # update means. An installed choice wins over a default, both ways.
      CONTEXT_MODE=native
      echo "Keeping the installed context mode: native. Pass CONTEXT_MODE=1m to change."
    fi
  fi
fi
# (die() is defined at the top of this file, above the root wall that is now the
# first refusal to call it. It moved out of the steps in v1.10.2 because of the
# port refusals below: a function defined under a call that fires printed
# "command not found" and lost the message. tests/test_install_preflight.py
# pins that, tests/test_install_root_refusal.py pins the wall.)

# PORT feeds the arithmetic below, so it is validated before it is used.
[[ "$PORT" =~ ^[0-9]+$ ]] || die "PORT must be a number (got '$PORT')"
if [ -z "$_ENV_PROXY_PORT" ] && [ -r "/etc/systemd/system/qwen38-keepalive.service" ]; then
  CUR_PROXY="$(grep -oE 'keepalive-proxy\.py [0-9]+' /etc/systemd/system/qwen38-keepalive.service | head -1 | tr -dc '0-9' || true)"
  [ -n "${CUR_PROXY:-}" ] && PROXY_PORT="$CUR_PROXY"
fi
PROXY_PORT="${PROXY_PORT:-$((PORT+1))}"

# A non-number dies here with its name on it, and the engine and its proxy may
# never share one (each check would see "its" port free, then both services
# would fight over the same socket at runtime).
[[ "$PROXY_PORT" =~ ^[0-9]+$ ]] || die "PROXY_PORT must be a number (got '$PROXY_PORT')"
[ "$PORT" != "$PROXY_PORT" ] || die "PORT and PROXY_PORT are both $PORT: the engine and its keepalive proxy cannot share a port"

if [ "$NO_SERVICE" -eq 1 ] && [ "$NO_START" -eq 1 ]; then
  printf -- '--no-start controls the systemd service; with --no-service there is no service (drop one flag)\n' >&2; exit 1
fi
if [ "$NO_OPENCODE" -eq 1 ] && [ "$WITH_OPENCODE" -eq 1 ]; then
  printf -- '--no-opencode and --with-opencode contradict each other (drop one flag)\n' >&2; exit 1
fi
# The opencode on/off choice persists like the other choices: a marker file in
# CONFIG_DIR, written by --no-opencode, removed by --with-opencode. The switch
# script and the cockpit read the same marker.
OC_OFF_MARK="$CONFIG_DIR/opencode.off"
OPENCODE=1
if [ "$NO_OPENCODE" -eq 1 ]; then
  OPENCODE=0
elif [ "$WITH_OPENCODE" -eq 0 ] && [ -f "$OC_OFF_MARK" ]; then
  OPENCODE=0
  echo "Keeping the opencode integration off (your earlier --no-opencode). Pass --with-opencode to re-enable."
fi
# The same refusal as the one near the top, once the lane is final: without
# MODEL_CHOICE that one reads LANE before the convergence has moved it to the
# installed flash lane, and step 6 then patched YaRN into the flash checkpoint's
# config.json in the shared HF cache (found in review, 2026-09-24).
if [ "$LANE" = "flash" ] && [ "$CONTEXT_MODE" = "1m" ]; then
  printf 'ERROR: CONTEXT_MODE=1m is a 27B mode, and this box serves the flash lane. Flash-Next serves\n' >&2
  printf '       its full native 262144 window. Drop CONTEXT_MODE, or pass a 27B MODEL_CHOICE with it.\n' >&2
  exit 1
fi
# The default context mode, resolved here because everything it depends on (the
# lane, --no-service, and what the installed unit already says) is only known
# now. An explicit CONTEXT_MODE still refuses the combinations it cannot serve,
# a few lines below; a default must never refuse anything.
if [ -z "$CONTEXT_MODE" ]; then
  if [ "$LANE" = "flash" ]; then
    CONTEXT_MODE=native
  elif [ "$NO_SERVICE" -eq 1 ]; then
    CONTEXT_MODE=native
    echo "--no-service installs the native 262144 window (1m needs the keepalive proxy service)."
  else
    CONTEXT_MODE=1m
    echo "Context mode: 1m (1,010,000 tokens). Pass CONTEXT_MODE=native for the 262144 window."
  fi
fi

if [ "$NO_COCKPIT" -eq 1 ] && [ "$WITH_COCKPIT" -eq 1 ]; then
  printf -- '--no-cockpit and --with-cockpit contradict each other (drop one flag)\n' >&2; exit 1
fi

# --no-start and --no-service both return before the image step, so the flag would be
# accepted and silently do nothing. Say so here rather than at the end of a long install.
if [ "$WITH_IMAGE" -eq 1 ] && { [ "$NO_START" -eq 1 ] || [ "$NO_SERVICE" -eq 1 ]; }; then
  printf -- '--with-image needs the full install: it installs a systemd unit and proves it serves.\n' >&2
  printf -- 'Run ./install.sh without --no-start/--no-service, or ./install-image.sh on its own.\n' >&2
  exit 1
fi
# Since v1.12 the cockpit is part of a plain install: the one-liner has to leave
# a box you can open and drive, not a box plus a second command to find in a
# README. The choice persists exactly like the opencode one, in a marker file,
# so --no-cockpit is not quietly undone by the next upgrade.
CK_OFF_MARK="$CONFIG_DIR/cockpit.off"
COCKPIT=1
if [ "$NO_COCKPIT" -eq 1 ]; then
  COCKPIT=0
  mkdir -p "$CONFIG_DIR"
  [ -f "$CK_OFF_MARK" ] || printf 'disabled with ./install.sh --no-cockpit on %s\n' "$(date -u +%Y-%m-%dT%H:%MZ)" > "$CK_OFF_MARK"
elif [ "$WITH_COCKPIT" -eq 1 ]; then
  rm -f "$CK_OFF_MARK"
elif [ -f "$CK_OFF_MARK" ]; then
  COCKPIT=0
  echo "Keeping the cockpit off (your earlier --no-cockpit). Pass --with-cockpit to re-enable."
fi
# --no-service means no systemd at all, and the cockpit is a systemd unit.
[ "$NO_SERVICE" -eq 1 ] && COCKPIT=0

if [ "$NO_SERVICE" -eq 1 ] && [ "$CONTEXT_MODE" = "1m" ]; then
  if [ -z "$_ENV_CONTEXT_MODE" ]; then
    # The 1m came from the installed unit, not from the operator: say so. The refusal
    # named a CONTEXT_MODE nobody had set (found in review, 2026-09-24). It stays a
    # refusal: a native --no-service install restores the configs that unit reads, and it
    # would crash at its next start.
    printf -- 'The installed 27B unit serves the 1M window, and --no-service installs the native 262144 one for\n./run.sh: the checkpoint configs would no longer match that unit, which would crash at its next start.\nDrop --no-service to update the service (it keeps 1m), or pass CONTEXT_MODE=native to move this box\nto native (then run a plain ./install.sh, so the unit follows).\n' >&2; exit 1
  fi
  printf -- 'CONTEXT_MODE=1m needs the systemd path (keepalive proxy service); ./run.sh serves the native config only.\nEither drop --no-service, or pass CONTEXT_MODE=native explicitly.\n' >&2; exit 1
fi
if [ "$NO_SERVICE" -eq 1 ] && [ "$LANE" = "flash" ]; then
  printf -- 'The flash targets are service-only in this release (the lane was validated as a systemd unit).\nDrop --no-service, or install one of the 27B targets for the foreground ./run.sh path.\n' >&2; exit 1
fi

step() { printf '\n\033[1;36m── %s\033[0m\n' "$*"; }
# True (0) when a service has to be restarted to run what is on disk: it is not running,
# or it started before the last change of one of the files it reads. The files are only
# rewritten when their content changes (cmp before install), so a date that moved is a
# change. A run that changed nothing restarted the proxy, cutting every request in flight
# through it, even with the engine kept (found in review, 2026-09-24).
stale_since(){
  local unit="$1" started f; shift
  [ "$(systemctl show -p ActiveState --value "$unit" 2>/dev/null)" = "active" ] || return 0
  started="$(systemctl show -p ExecMainStartTimestamp --value --timestamp=unix "$unit" 2>/dev/null | tr -dc '0-9')"
  [ -n "$started" ] || return 0
  for f in "$@"; do
    [ -e "$f" ] && [ "$(stat -L -c %Y "$f")" -gt "$started" ] && return 0
  done
  return 1
}
# (die() is defined above, before the port validations that call it first.)

step "1/10 Preflight checks"
[ "$(uname -m)" = "aarch64" ] || die "This setup targets GB10 (aarch64). Detected: $(uname -m)."
command -v nvidia-smi >/dev/null || die "nvidia-smi not found. Is the NVIDIA driver stack installed? (stock on DGX OS)"
# nvidia-smi says what is wrong when it cannot reach the driver, and exits non-zero:
# inside a bare $(...) under set -e that ended the install with only "Install failed at
# line N", its own explanation captured into a variable nobody printed (found in review,
# 2026-09-24).
if ! GPU_OUT="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>&1)"; then
  die "nvidia-smi cannot reach the GPU: $(printf '%s' "$GPU_OUT" | tr '\n' ' ' | cut -c1-300). The NVIDIA driver is probably not loaded; after a kernel or driver update, a reboot loads it ('nvidia-smi' alone shows the same error)."
fi
GPU_NAME="$(printf '%s\n' "$GPU_OUT" | head -1)"
echo "GPU: $GPU_NAME"
case "$GPU_NAME" in *GB10*) ;; *) echo "WARNING: expected GB10, found '$GPU_NAME'. Continuing, but this config was only validated on GB10 (memory sizing may not fit other GPUs)." ;; esac
command -v docker >/dev/null || die "docker not found. Install Docker + NVIDIA Container Toolkit (stock on DGX OS)."
command -v python3 >/dev/null || die "python3 not found on the host (needed for the template patcher; stock on DGX OS)."
command -v ss >/dev/null || die "ss not found (iproute2, stock on DGX OS): the preflight cannot check the ports without it."
docker info >/dev/null 2>&1 || die "Cannot talk to the docker daemon. Fix: sudo usermod -aG docker \$USER && re-login (or run with a user in the docker group)."
# /proc/meminfo, not `free`: free(1) localizes its row labels (issue #3)
TOTAL_GB=$(awk '/^MemTotal/{print int($2/1048576)}' /proc/meminfo)
[ "$TOTAL_GB" -ge 110 ] || die "This config needs a ~121 GB unified-memory machine; found ${TOTAL_GB} GB."
# Measured where the weights actually land: HF_CACHE may live on another disk,
# and then free space under $HOME says nothing (the error message used to send
# people there while measuring here).
mkdir -p "$HF_CACHE" 2>/dev/null || true
FREE_DISK_GB=$(df -BG --output=avail "$HF_CACHE" 2>/dev/null | tail -1 | tr -dc '0-9')
# Fresh installs need ~45 GB for the 27B stack (checkpoints + caches) and
# ~180 GB for Flash-Next (its NVFP4 checkpoint alone is ~135 GB), plus the 50 GB
# its PLE table takes below. Upgrades with the checkpoint already cached only need
# working room. README "Quickstart" states these numbers: keep the two together.
NEED_GB=45; DOCKER_NEED_GB=40; IMG_LABEL="33 GB Docker image"
if [ "$LANE" = "flash" ]; then
  NEED_GB=180; DOCKER_NEED_GB=35; IMG_LABEL="30 GB Docker image"
fi
# What is left to download, not what a checkpoint weighs. huggingface_hub creates the
# snapshot folder before the first byte of the first file (file_download.py, 1.31.0), so a
# folder there said nothing, and a flash download interrupted at 74 GB of 124 was checked
# against 10 on its resume (found in review, 2026-09-24). A snapshot is cached when every
# shard its index names is in it, since a file appears there only once its blob is whole;
# otherwise what the cache already holds for this repo comes off the need.
ckpt_cached(){  # $1 = the repo's cache folder, $2 = the revision (a commit or a ref name)
  python3 - "$1" "$2" <<'PY'
import json, pathlib, sys
repo, rev = pathlib.Path(sys.argv[1]), sys.argv[2]
ref = repo / "refs" / rev
snap = repo / "snapshots" / (ref.read_text().strip() if ref.is_file() else rev)
try:
    shards = set(json.loads((snap / "model.safetensors.index.json").read_text())["weight_map"].values())
except (OSError, ValueError, KeyError, TypeError, AttributeError):
    shards = {"model.safetensors"}
sys.exit(0 if shards and all((snap / s).exists() for s in shards) else 1)
PY
}
HF_REPO_CACHE="$HF_CACHE/hub/models--${MODEL_REPO//\//--}"
if ckpt_cached "$HF_REPO_CACHE" "$MODEL_REV"; then
  NEED_GB=10
else
  HAVE_B="$({ du -s --apparent-size -B1 "$HF_REPO_CACHE/blobs" 2>/dev/null || true; } | cut -f1)"
  NEED_GB=$((NEED_GB - ${HAVE_B:-0} / 1073741824)); [ "$NEED_GB" -ge 10 ] || NEED_GB=10   # floor: never under the need
fi
if [ "$LANE" = "flash" ] && ! ls "$PLE_DIR"/ple_table_*.bin >/dev/null 2>&1; then
  # The 47.7 GiB sparse backing file is written on every boot, in PLE_DIR; the space
  # has to be there whether or not a previous boot left one behind, and on that disk:
  # it was counted against HF_CACHE's, which says nothing when PLE_DIR is elsewhere.
  PLE_PARENT="$PLE_DIR"; while [ ! -e "$PLE_PARENT" ]; do PLE_PARENT="$(dirname "$PLE_PARENT")"; done
  if [ "$(stat -c %d "$PLE_PARENT")" = "$(stat -c %d "$HF_CACHE")" ]; then
    NEED_GB=$((NEED_GB + 50))
  else
    PLE_FREE_GB="$({ df -BG --output=avail "$PLE_PARENT" 2>/dev/null || true; } | tail -1 | tr -dc '0-9')"
    [ -n "$PLE_FREE_GB" ] && [ "$PLE_FREE_GB" -ge 50 ] || die "Need ~50 GB free under PLE_DIR=$PLE_DIR for the flash lane's PLE table (47.7 GiB, written at every boot); found ${PLE_FREE_GB:-unknown} GB. Free some space or set PLE_DIR to another disk."
  fi
fi
[ -n "$FREE_DISK_GB" ] && [ "$FREE_DISK_GB" -ge "$NEED_GB" ] || die "Need ~${NEED_GB} GB free for the checkpoints and caches under $HF_CACHE; found ${FREE_DISK_GB:-unknown} GB. Free some space or set HF_CACHE to another disk."
DOCKER_ROOT=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)
DOCKER_FREE_GB=$(df -BG --output=avail "$DOCKER_ROOT" 2>/dev/null | tail -1 | tr -dc '0-9')
# What step 2 pulls, decided here because the room it needs depends on it. An
# OVERLAY_FLASH=1 install builds on the 2026-08-26 base, not on the image the lane
# serves by default, so it is that one that has to be here.
if [ "$LANE" = "flash" ]; then
  PULL_TARGET="$FLASH_IMAGE"
  [ "$OVERLAY_FLASH" = "1" ] && PULL_TARGET="$OVERLAY_FLASH_BASE_IMAGE"
else
  PULL_TARGET="$IMAGE"
fi
# An image already here needs no room for itself, only the container's: an update
# on a box with 36 GB free was refused for want of 40, with the image in place and
# nothing to download (reference box, 2026-09-23).
if docker image inspect "$PULL_TARGET" >/dev/null 2>&1; then
  DOCKER_NEED_GB=5; IMG_LABEL="container (its image is already here)"
fi
[ "${DOCKER_FREE_GB:-0}" -ge "$DOCKER_NEED_GB" ] || die "Need ~${DOCKER_NEED_GB} GB free on $DOCKER_ROOT for the $IMG_LABEL; found ${DOCKER_FREE_GB:-?} GB (docker images live there, not under \$HOME)."
if ss -tlnH 2>/dev/null | awk '{print $4}' | grep -q ":$PORT\$"; then
  # The port may be held by either of OUR engines: same-engine reinstall
  # (converge) or a cross-engine switch (the old engine is stopped at step 9).
  if docker inspect qwen38-sglang --format '{{join .Args " "}}' 2>/dev/null | grep -qE -- "--port ${PORT}(\s|$)"; then
    echo "Note: qwen38-sglang is already running on :$PORT, installing over it ($([ "$LANE" = flash ] && echo 'lane switch at the final step' || echo 'converging config'))."
  elif docker inspect qwen38-flash --format '{{join .Args " "}}' 2>/dev/null | grep -qE -- "--port ${PORT}(\s|$)" \
       || docker inspect qwen38-flash --format '{{json .HostConfig.PortBindings}}' 2>/dev/null | grep -q "\"${PORT}\""; then
    echo "Note: qwen38-flash is already running on :$PORT, installing over it ($([ "$LANE" = 27b ] && echo 'lane switch at the final step' || echo 'converging config'))."
  else
    die "Port $PORT is already in use by another program (see: ss -tlnp | grep :$PORT). Free it, or install with PORT=<other> ./install.sh"
  fi
fi
if [ "$NO_SERVICE" -eq 0 ] && ss -tlnH 2>/dev/null | awk '{print $4}' | grep -q ":$PROXY_PORT\$"; then
  if systemctl is-active --quiet qwen38-keepalive 2>/dev/null; then
    echo "Note: the keepalive proxy is already running on :$PROXY_PORT, re-installing over it."
  else
    die "Port $PROXY_PORT (keepalive proxy) is already in use by another program. Free it, or install with PROXY_PORT=<other>"
  fi
fi
# sudo is needed from step 8 on, and it is asked for here, before the downloads rather
# than after them: `sudo -n` alone never prompts, and a fresh box where sudo wants a
# password (DGX OS does) died at step 8 after ~20 min of pulls (found in review,
# 2026-09-24). With a terminal sudo asks now; without one (a background run) `sudo -v`
# fails at once, and refusing now costs nothing where refusing at step 8 cost the pulls.
if [ "$NO_SERVICE" -eq 0 ] && ! sudo -n true 2>/dev/null; then
  echo "sudo is needed from step 8 on (systemd units); it asks for your password now, before the downloads."
  sudo -v || die "sudo could not be used here: run 'sudo -v' in this terminal, then re-run ./install.sh"
fi
echo "OK (aarch64, ${TOTAL_GB} GB RAM, ${FREE_DISK_GB} GB free)"

if [ "$LANE" = "flash" ]; then
  step "2/10 Pulling the official SGLang Flash-Next image (~30 GB, one-time, resumable)"
  docker pull "$PULL_TARGET" || die "docker pull failed. Causes: no internet, Docker Hub rate limit (retry in a few minutes or 'docker login'), or the pinned digest was removed upstream: try FLASH_IMAGE=lmsysorg/sglang:dev-qwen38-next-local ./install.sh"
  PULLED_IMAGE="$PULL_TARGET"
else
  step "2/10 Pulling the SGLang image (~39 GB, one-time, resumable)"
  docker pull "$PULL_TARGET" || die "docker pull failed. Causes: no internet, Docker Hub rate limit (retry in a few minutes or 'docker login'), or the pinned digest was removed upstream: try IMAGE=lmsysorg/sglang:v0.5.19 ./install.sh, the moving tag of the same release"
  PULLED_IMAGE="$PULL_TARGET"
fi
# A digest pull leaves the image with no tag, and to Docker an image with no tag is
# dangling: `docker image prune` or `docker system prune`, the usual way to win back room
# on a full disk, deletes it, and the lane pulls 30 GB again at its next start (both
# serving images of the reference box were listed dangling, 2026-09-23). A local tag takes
# it out of that set, for both lanes' pins when they are here. uninstall.sh knows the name.
pin_tag() {   # $1 lane, $2 image: a digest reference present on this box gets its local tag
  local tag id
  case "$2" in *@sha256:*) ;; *) return 0 ;; esac
  id="$(docker image inspect "$2" --format '{{.Id}}' 2>/dev/null)" || return 0   # not on this box
  tag="qwen38-pinned:$1-$(printf '%s' "${2##*@sha256:}" | cut -c1-12)"
  if [ "$(docker image inspect "$tag" --format '{{.Id}}' 2>/dev/null || true)" = "$id" ]; then
    return 0   # already tagged
  fi
  if docker tag "$2" "$tag"; then
    echo "tagged $tag, so a docker image prune leaves it alone"
  else
    echo "NOTE: could not tag $2; a docker image prune would delete it"
  fi
}
pin_tag "$LANE" "$PULLED_IMAGE"
if [ "$LANE" = "flash" ]; then pin_tag 27b "$IMAGE"; else pin_tag flash "$FLASH_IMAGE"; fi

step "3/10 Verifying the container can see the GPU"
# --entrypoint: the image ships NVIDIA's own entrypoint script
# (/opt/nvidia/nvidia_entrypoint.sh), which prints a banner and runs its argument;
# overriding it is what makes this a plain nvidia-smi call with plain output.
GPU_SEEN="$(docker run --rm --gpus all --entrypoint nvidia-smi "$PULLED_IMAGE" -L 2>/dev/null | grep -m1 '^GPU' || true)"
[ -n "$GPU_SEEN" ] \
  || die "'docker run --gpus all' cannot see the GPU (no GPU line from nvidia-smi -L in the container). The NVIDIA Container Toolkit is missing or unconfigured. Fix: sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker"
echo "OK, container sees: $GPU_SEEN"

if [ "$LANE" = "flash" ]; then
  step "4/10 Downloading the Flash-Next checkpoint (~136 GB, one-time, reuses/resumes any local copy)"
  echo "This is the big one: at 100 MB/s it takes ~25 min. Interrupting is safe, re-running resumes."
else
  step "4/10 Downloading checkpoints (~28 GB, one-time, reuses/resumes any local copy)"
fi
mkdir -p "$HF_CACHE" "$CONFIG_DIR/sglang-cache"
# A kept custom model is already serving from this cache: skip its download.
DL_MODEL_REPO="$MODEL_REPO"
[ "$KEEP_MODEL_VERBATIM" -eq 1 ] && DL_MODEL_REPO=""
# Flash has no separate draft checkpoints: its MTP head ships inside the
# target checkpoint itself. Empty repo vars are skipped by the downloader.
DL_DRAFT2_REPO="$DRAFT2_REPO"
if [ "$LANE" = "flash" ]; then DL_DRAFT2_REPO=""; fi
# Unauthenticated downloads get throttled by the Hub (measured: a fresh-cache
# pull stalled at 3.3 GB). A token in $HF_CACHE/token is picked up through the
# mount; HF_TOKEN in the environment is passed through as well.
DL_TOKEN_ARGS=()
[ -n "${HF_TOKEN:-}" ] && DL_TOKEN_ARGS=(-e HF_TOKEN="$HF_TOKEN")
# HF_HUB_DISABLE_XET: the hub library's Xet transfer backend stalled silently
# during the release campaign (ESTAB socket, zero bytes, forever; 0-8 MB/s
# when moving at all) while the classic CDN path measured 89 MB/s on the same
# box, same second. HF_HUB_DOWNLOAD_TIMEOUT turns any remaining silent stall
# into a ReadTimeout that the retry loop below resumes from.
docker run --rm -i --network host --user "$(id -u):$(id -g)" \
  --entrypoint python3 \
  -e HF_HOME=/hf -e HF_HUB_DOWNLOAD_TIMEOUT=30 -e HF_HUB_DISABLE_XET=1 \
  -e MODEL_REPO="$DL_MODEL_REPO" -e MODEL_REV="$MODEL_REV" \
  -e DRAFT2_REPO="$DL_DRAFT2_REPO" -e DRAFT2_REV="$DRAFT2_REV" \
  "${DL_TOKEN_ARGS[@]}" \
  -v "$HF_CACHE":/hf \
  "$PULLED_IMAGE" - <<'PYEOF' || die "Checkpoint download failed. Causes: no internet, HuggingFace throttling of unauthenticated downloads (set HF_TOKEN=<your token>, or re-run: downloads resume), a pinned revision removed (try MODEL_REV=main DRAFT_REV=main ./install.sh), or a permission error: if your $HF_CACHE contains root-owned files from other tools, fix with: sudo chown -R \$(id -u):\$(id -g) $HF_CACHE"
import os
import time
from huggingface_hub import constants, snapshot_download


def download(repo, rev):
    """snapshot_download, with the one exception to the Xet ban above.

    The classic CDN refuses any single file over MAX_HTTP_DOWNLOAD_SIZE
    (50,000,000,000 bytes) and says so in a ValueError naming hf_xet. One target
    ships such a file: nvidia/Qwen3.8-Flash-Next-NVFP4 carries its n-gram table
    as one 53.7 GB safetensors. Without this, that target cannot be fetched at
    all: the resume loop retries the refusal four times and the run dies under a
    message naming four causes that are all the wrong one, over a traceback that
    does name hf_xet (seen 2026-09-18, 74 GB of the 124 left in cache). Xet is
    turned back on only for the repo the CDN just refused, and turned off again
    right after, so every other file keeps the transfer path that measured
    89 MB/s against its 0-8.
    """
    try:
        return snapshot_download(repo, revision=rev)
    except Exception as e:
        if "too large to be downloaded" not in str(e) or not constants.HF_HUB_DISABLE_XET:
            raise
        print("a file is over the classic CDN limit; retrying this repo with Xet", flush=True)
        constants.HF_HUB_DISABLE_XET = False
        try:
            return snapshot_download(repo, revision=rev)
        finally:
            constants.HF_HUB_DISABLE_XET = True


for repo, rev in ((os.environ["MODEL_REPO"], os.environ["MODEL_REV"]),
                  (os.environ["DRAFT2_REPO"], os.environ["DRAFT2_REV"])):
    if not repo:  # kept custom model: already in cache, nothing to download
        continue
    print(f"── {repo} @ {rev}", flush=True)
    for attempt in range(1, 6):  # a resumed attempt reuses every finished byte
        try:
            path = download(repo, rev)
            break
        except Exception as e:
            if attempt == 5:
                raise
            print(f"download interrupted ({type(e).__name__}), resuming ({attempt}/5)...", flush=True)
            time.sleep(10)
    # A pinned-sha download writes no refs/main; serving later with
    # HF_HUB_OFFLINE=1 (the 1m unit) resolves "main" through that file and
    # would fail on a fresh machine. Write it once, never overwrite.
    sha = os.path.basename(path.rstrip("/"))
    ref = os.path.join(os.path.dirname(os.path.dirname(path.rstrip("/"))), "refs", "main")
    if len(sha) == 40 and not os.path.exists(ref):
        os.makedirs(os.path.dirname(ref), exist_ok=True)
        with open(ref, "w") as f:
            f.write(sha)
print("checkpoints ready", flush=True)
PYEOF

LANE_OVERLAY=0; [ "$LANE" = "flash" ] && LANE_OVERLAY="$OVERLAY_FLASH"
if [ "$LANE_OVERLAY" != "1" ]; then
  # Say the image the unit will actually carry, not the pin it came from: those
  # are the same thing on a default install and different the moment someone
  # passes SERVE_IMAGE=, which is the one case where this line is the only
  # on-screen confirmation that the override took.
  LANE_SERVE_IMAGE="$([ "$LANE" = flash ] && echo "$FLASH_SERVE_IMAGE" || echo "$SERVE_IMAGE")"
  LANE_PIN_IMAGE="$([ "$LANE" = flash ] && echo "$FLASH_IMAGE" || echo "$IMAGE")"
  if [ "$LANE_SERVE_IMAGE" = "$LANE_PIN_IMAGE" ]; then
    step "5/10 Serving image: the pinned official one, nothing to build"
    echo "$LANE_SERVE_IMAGE"
  else
    step "5/10 Serving image: an operator override, nothing to build"
    echo "$LANE_SERVE_IMAGE   (SERVE_IMAGE=, instead of the pin $LANE_PIN_IMAGE)"
    # Step 2 pulls the pin, never an override, and the overlay build that used to
    # guarantee this tag existed is gone since v1.14. Without this check the
    # install completes green, writes and enables the unit, and the engine then
    # loops on an unpullable local tag for the full 20-minute health wait.
    docker image inspect "$LANE_SERVE_IMAGE" >/dev/null 2>&1 \
      || die "serving image not present: $LANE_SERVE_IMAGE. Nothing pulls an override, so build or pull it first, or drop SERVE_IMAGE= to serve the pin ($LANE_PIN_IMAGE)."
  fi
  if [ "$LANE" = flash ]; then
    echo "OVERLAY_FLASH=1 ./install.sh rebuilds the local overlay image of v1.7 instead (the rollback)."
  fi
else
  step "5/10 Building the Flash-Next overlay image (OVERLAY_FLASH=1: pinned base + verified files + gate checks, offline, ~2 min)"
  BASE_IMAGE="$OVERLAY_FLASH_BASE_IMAGE" TAG="$FLASH_SERVE_IMAGE" "$REPO_DIR/flash-sglang/build-image.sh" \
    || die "Flash overlay image build failed: see flash-sglang/ATTRIBUTION.md; the checksums and in-image checks run before tagging, so a failure means a corrupted checkout (git status) or an upstream image layout change. The overlay is the rollback path: the default install needs no build."
fi

# The reduced draft vocabulary. Built inside the serving image, so the tokenizer
# that ranks the ids is the one the served model ships, not whatever the host has
# (the host has no transformers at all). Corpus-free by default: the ranking
# falls back to the tokenizer's own construction order, which is a frequency
# ranking learned over the tokenizer's training corpus. Drop a corpus.jsonl in
# the config directory (one JSON object per line with a "text" field, ideally the
# model's own output) and it is used as the primary ranking instead, which is
# what the model's drafter actually has to predict.
if [ "$LANE" = "flash" ] && [ "${SPEC_TOKEN_MAP_SIZE:-0}" -gt 0 ]; then
  if [ -s "$CONFIG_DIR/$TOKEN_MAP_NAME" ]; then
    echo "reduced draft vocabulary already built: $CONFIG_DIR/$TOKEN_MAP_NAME"
  else
    echo "building the reduced draft vocabulary ($SPEC_TOKEN_MAP_SIZE ids, ~30 s)"
    # the commit a ref names (MODEL_REV=main): snapshots/main never exists, and the map was
    # skipped on every run (found in review, 2026-09-24)
    MAP_REPO_CACHE="$HF_CACHE/hub/models--${MODEL_REPO//\//--}"
    MAP_REV="$MODEL_REV"; [ -f "$MAP_REPO_CACHE/refs/$MODEL_REV" ] && MAP_REV="$(cat "$MAP_REPO_CACHE/refs/$MODEL_REV")"
    SNAP_DIR="$(ls -d "$MAP_REPO_CACHE/snapshots/$MAP_REV" 2>/dev/null || true)"
    if [ -z "$SNAP_DIR" ]; then
      echo "NOTE: no snapshot at the pinned revision yet; the draft vocabulary is skipped this run."
      echo "      Re-run ./install.sh after the checkpoint is in place to build and serve it."
    else
      MAP_CORPUS=""
      [ -s "$CONFIG_DIR/corpus.jsonl" ] && MAP_CORPUS="--corpus /out/corpus.jsonl"
      docker run --rm --entrypoint python3 \
        -v "$HF_CACHE":/root/.cache/huggingface \
        -v "$CONFIG_DIR":/out \
        -v "$REPO_DIR":/repo:ro \
        "$([ "$OVERLAY_FLASH" = "1" ] && echo "$FLASH_SERVE_IMAGE" || echo "$FLASH_IMAGE")" \
        /repo/build-token-map.py \
          --snapshot "/root/.cache/huggingface/hub/models--${MODEL_REPO//\//--}/snapshots/$MAP_REV" \
          --out "/out/$TOKEN_MAP_NAME" --size "$SPEC_TOKEN_MAP_SIZE" $MAP_CORPUS \
        || die "building the reduced draft vocabulary failed. It is an optimization, not a requirement: re-run with SPEC_TOKEN_MAP_SIZE=0 to serve without it, and please open an issue with the output above."
    fi
  fi
fi

# What the engine this run installs reads when it starts, as it stands before this run
# writes any of it, and whether the running engine already has exactly that. Step 9
# restarts the engine only when one of the two says otherwise: an update used to cost a
# full boot (8 min on the 27B, 12 on flash) even when it changed nothing the engine reads.
ENGINE_UNIT_PATH="$SGL_UNIT_PATH"; [ "$LANE" = "flash" ] && ENGINE_UNIT_PATH="$FLASH_UNIT_PATH"
ENGINE_CKPTS=("$MODEL_REPO@$MODEL_REV")
[ "$LANE" = "27b" ] && ENGINE_CKPTS+=("$DRAFT2_REPO@$DRAFT2_REV")
ENGINE_FP_BEFORE=""; ENGINE_RUNNING_IT="no: not installed yet"
if [ -f "$ENGINE_UNIT_PATH" ]; then
  ENGINE_FP_BEFORE="$(python3 "$REPO_DIR/engine-inputs.py" fingerprint "$ENGINE_UNIT_PATH" "$CONFIG_DIR" "$HF_CACHE" "${ENGINE_CKPTS[@]}" 2>/dev/null || true)"
  ENGINE_RUNNING_IT="$(python3 "$REPO_DIR/engine-inputs.py" running "$ENGINE_UNIT_PATH" "$CONFIG_DIR" "$HF_CACHE" "${ENGINE_CKPTS[@]}" 2>/dev/null || echo "no: could not tell")"
fi

step "6/10 API key + patched chat template"
if [ ! -s "$CONFIG_DIR/api-key" ]; then
  # Subshell umask like install-agent.sh: with umask 022 the file would be
  # born 644 and world-readable until the chmod below runs.
  ( umask 077 && head -c 24 /dev/urandom | base64 | tr -d '/+=' > "$CONFIG_DIR/api-key" ) \
    || die "could not write $CONFIG_DIR/api-key"
  chmod 600 "$CONFIG_DIR/api-key"
  echo "API key generated at $CONFIG_DIR/api-key"
else
  echo "API key already present, keeping it"
fi
KEY="$(cat "$CONFIG_DIR/api-key")"   # used by the step-9 smoke test
# One patched template per engine file name: the served template always follows
# the served model (both fixes: reasoning_effort normalization + mid-conversation
# system messages as <system-reminder> blocks; see patch-template.py).
TEMPLATE_OUT="$CONFIG_DIR/chat-template-sglang.jinja"
[ "$LANE" = "flash" ] && TEMPLATE_OUT="$CONFIG_DIR/chat-template-flashnext.jinja"
if [ "$KEEP_MODEL_VERBATIM" -eq 1 ]; then
  [ -s "$TEMPLATE_OUT" ] \
    || die "custom model kept, but no patched template at $TEMPLATE_OUT. Pass MODEL_CHOICE=stock, uncensored, fp8 or uncensored-fp8 to regenerate it."
  echo "custom target model kept: existing patched template left untouched"
else
  python3 "$REPO_DIR/patch-template.py" "$HF_CACHE" "$TEMPLATE_OUT" "$MODEL_REV" "$MODEL_REPO" \
    || die "Template patch failed (see message above). If the upstream template changed, please open an issue on this repo."
fi
# The checkpoint configs the context mode needs, YaRN patched in or restored. Written right
# before the unit that reads them, never at this step: a run that died between the two
# (the opencode download of step 7, a sudo that could no longer ask at step 8) left the
# installed unit of the old mode on configs of the new one, and a unit on configs of the
# other mode crashes at load, the next time systemd starts it (found in review,
# 2026-09-24). Called at step 8 just before the unit is installed, or at the end of step 7
# on a --no-service install, which writes no unit.
apply_context_configs(){
  if [ "$CONTEXT_MODE" = "1m" ]; then
    # Both configs must carry the YaRN patch (target AND draft, or the draft
    # crashes at load). Idempotent; originals backed up as config.json.pre-yarn.
    # A kept custom model has no known pin: its newest cached snapshot is patched.
    if [ "$KEEP_MODEL_VERBATIM" -eq 1 ]; then
      python3 "$REPO_DIR/patch-yarn.py" "$HF_CACHE" "$MODEL_REPO" || die "YaRN patch failed on the target model"
    else
      python3 "$REPO_DIR/patch-yarn.py" "$HF_CACHE" "$MODEL_REPO" "$MODEL_REV" || die "YaRN patch failed on the target model"
    fi
    python3 "$REPO_DIR/patch-yarn.py" "$HF_CACHE" "$DRAFT2_REPO" "$DRAFT2_REV" || die "YaRN patch failed on the DFlash2 draft"
  else
    # Coming home from 1m: a native server crashes at load on a YaRN-patched
    # config (measured 2026-09-11: target context_length 1010000 against a
    # derived 262144), so a native install restores the pre-YaRN originals.
    # Refuses with a re-download fix-it when a config is patched but its backup
    # is gone. Flash configs are never patched (1m is a 27B mode), so only the
    # 27B lane restores.
    #
    # One combination leaves a box that boots into a crash, and it is quiet about
    # it: --no-service returns at the end of step 7, so an installed 1m unit keeps
    # asking for 1,010,000 from a config this restore just put back to 262,144,
    # and the engine dies at load the next time systemd starts it. Seen here on
    # 2026-09-18 while testing a native install against a 1m box's cache.
    # ONLY --no-service. --no-start reaches step 8, renders the native template
    # over that unit and enables it, so nothing is stranded there and warning
    # about it would be a lie with two wrong remedies attached (caught in review
    # the day this guard was written).
    if [ "$LANE" = "27b" ] && [ "$NO_SERVICE" -eq 1 ] && [ -r "$SGL_UNIT_PATH" ]; then
      _INSTALLED_CTX="$(grep -oE -- '--context-length [0-9]+' "$SGL_UNIT_PATH" 2>/dev/null | awk '{print $2}' | head -1 || true)"
      if [ -n "$_INSTALLED_CTX" ] && [ "$_INSTALLED_CTX" -gt 262144 ]; then
        echo "WARNING: the installed unit serves --context-length $_INSTALLED_CTX, and this native"
        echo "         install is about to restore the pre-YaRN configs it reads. --no-service writes"
        echo "         no unit, so that unit would crash at load the next time it starts."
        echo "         Either re-run without --no-service (the unit is rewritten native),"
        echo "         or put the box back with: CONTEXT_MODE=1m ./install.sh"
      fi
    fi
    if [ "$LANE" = "27b" ]; then
      if [ "$KEEP_MODEL_VERBATIM" -eq 1 ]; then
        python3 "$REPO_DIR/patch-yarn.py" --restore "$HF_CACHE" "$MODEL_REPO" || die "YaRN restore failed on the kept model"
      else
        python3 "$REPO_DIR/patch-yarn.py" --restore "$HF_CACHE" "$MODEL_REPO" "$MODEL_REV" || die "YaRN restore failed on the target model"
      fi
      python3 "$REPO_DIR/patch-yarn.py" --restore "$HF_CACHE" "$DRAFT2_REPO" "$DRAFT2_REV" || die "YaRN restore failed on the DFlash2 draft"
    fi
  fi
}


if [ "$OPENCODE" -eq 0 ]; then
  step "7/10 opencode integration: off"
  mkdir -p "$CONFIG_DIR"
  [ -f "$OC_OFF_MARK" ] || printf 'disabled with ./install.sh --no-opencode on %s\n' "$(date -u +%Y-%m-%dT%H:%MZ)" > "$OC_OFF_MARK"
  if grep -q 'dgx-spark-qwen38' "$HOME/.local/bin/oc" 2>/dev/null; then
    rm -f "$HOME/.local/bin/oc"; echo "removed this repo's oc launcher"
  fi
  rm -f "$CONFIG_DIR/opencode.json"
  echo "no generated config, no launcher; switch-model.sh leaves your opencode default model alone."
  echo "Your own ~/.config/opencode/opencode.json is never touched either way. Re-enable: ./install.sh --with-opencode"
else
rm -f "$OC_OFF_MARK"
step "7/10 opencode provider config + oc launcher"
# What opencode-web reads, as it stands before this step writes any of it: the server is
# restarted at the end of the step only when one of them changed (see there).
oc_configs_sum(){ cat "$CONFIG_DIR/opencode.json" "$HOME/.config/opencode/opencode.json" 2>/dev/null | sha256sum; }
OC_SUM_BEFORE="$(oc_configs_sum)"
# ── opencode itself, at the pinned version (see the OPENCODE_VERSION pin) ─────────
# Absent: the release asset is downloaded, checked against its pinned sha256 and put
# where opencode's own installer puts it. Older in that place: replaced the same way.
# Older elsewhere (npm, brew, bun): opencode's own upgrader, which knows its method.
# Newer: kept, and said so, because going back a version can leave sessions a newer
# opencode wrote unreadable. None of it fails the install: opencode is one integration.
OC_HOME_BIN="$HOME/.opencode/bin/opencode"
OC_HOME_DIR="${OC_HOME_BIN%/opencode}"
oc_version(){ { "$1" --version 2>/dev/null || true; } | tail -1 | tr -d 'v[:space:]'; }
ver_lt(){ [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -1)" = "$1" ]; }
oc_fetch_pinned(){   # prints the cause of a failure; the caller says what it leaves behind
  local tmp url="https://github.com/anomalyco/opencode/releases/download/v$OPENCODE_VERSION/opencode-linux-arm64.tar.gz"
  tmp="$(mktemp -d)"
  if ! curl -fsSL --retry 3 -m 600 -o "$tmp/oc.tar.gz" "$url"; then
    rm -rf "$tmp"; echo "NOTE: could not download opencode $OPENCODE_VERSION ($url)"; return 1
  fi
  if ! printf '%s  %s\n' "$OPENCODE_SHA256" "$tmp/oc.tar.gz" | sha256sum -c --quiet - >/dev/null 2>&1; then
    rm -rf "$tmp"; echo "NOTE: the opencode $OPENCODE_VERSION download does not match its pinned sha256, so it was not installed"; return 1
  fi
  if ! tar -xzf "$tmp/oc.tar.gz" -C "$tmp" opencode; then
    rm -rf "$tmp"; echo "NOTE: the opencode $OPENCODE_VERSION archive did not unpack"; return 1
  fi
  mkdir -p "$OC_HOME_DIR"
  # a new file renamed over the old one: a server still running the old binary keeps it
  install -m 755 "$tmp/opencode" "$OC_HOME_BIN.new" && mv -f "$OC_HOME_BIN.new" "$OC_HOME_BIN"
  rm -rf "$tmp"
}
if [ -n "$_ENV_OPENCODE_VERSION" ] && [ -z "$_ENV_OPENCODE_SHA256" ]; then
  die "OPENCODE_VERSION=$OPENCODE_VERSION needs OPENCODE_SHA256 too (GitHub's digest of opencode-linux-arm64.tar.gz for that release): a version with no checksum is not a pin"
fi
OC_FOUND="$(command -v opencode || true)"
OC_ON_PATH="$OC_FOUND"   # what a shell of the user's finds, before the fallback below
[ -z "$OC_FOUND" ] && [ -x "$OC_HOME_BIN" ] && OC_FOUND="$OC_HOME_BIN"
OC_WAS_ABSENT=0; [ -z "$OC_FOUND" ] && OC_WAS_ABSENT=1
OC_HAVE=""; [ -n "$OC_FOUND" ] && OC_HAVE="$(oc_version "$OC_FOUND")"
if [ "$OPENCODE_PIN" != "1" ]; then
  echo "opencode ${OC_HAVE:-not installed}: kept as it is (OPENCODE_PIN=$OPENCODE_PIN)"
elif [ "$OC_HAVE" = "$OPENCODE_VERSION" ]; then
  echo "opencode $OC_HAVE: the version this repo tests"
elif [ -z "$OC_FOUND" ] || { [ "$OC_FOUND" -ef "$OC_HOME_BIN" ] && ver_lt "$OC_HAVE" "$OPENCODE_VERSION"; }; then
  if [ -z "$OC_FOUND" ]; then
    echo "opencode is not installed: installing $OPENCODE_VERSION (the release asset, checked against its pinned sha256)"
  else
    echo "opencode $OC_HAVE -> $OPENCODE_VERSION: replacing it with the release asset, checked against its pinned sha256"
  fi
  if oc_fetch_pinned; then
    OC_FOUND="$OC_HOME_BIN"; OC_HAVE="$(oc_version "$OC_HOME_BIN")"
    echo "opencode $OC_HAVE installed at $OC_HOME_BIN"
  elif [ -z "$OC_FOUND" ]; then
    echo "      opencode is still not installed, and the Agent tab and the oc launcher need it. Re-run ./install.sh"
    echo "      once GitHub is reachable, or install it from https://opencode.ai (this repo tests $OPENCODE_VERSION)."
  else
    echo "      opencode stays at $OC_HAVE."
  fi
elif ver_lt "$OC_HAVE" "$OPENCODE_VERSION"; then
  echo "opencode $OC_HAVE -> $OPENCODE_VERSION through opencode's own upgrader ($OC_FOUND)"
  "$OC_FOUND" upgrade "$OPENCODE_VERSION" </dev/null >/dev/null 2>&1 \
    || echo "NOTE: opencode upgrade $OPENCODE_VERSION failed; run it yourself"
  OC_HAVE="$(oc_version "$OC_FOUND")"
else
  echo "NOTE: opencode $OC_HAVE is newer than the $OPENCODE_VERSION this repo tests. Kept: going back"
  echo "      a version can leave sessions a newer opencode wrote unreadable. OPENCODE_PIN=0 silences this."
fi
if [ "$OPENCODE_PIN" = "1" ] && [ -n "$OC_HAVE" ] && ver_lt "$OC_HAVE" "$OPENCODE_VERSION"; then
  echo "NOTE: opencode is still $OC_HAVE after the step above; oc and the Agent tab keep running it"
fi
# The rest of this run (the Agent tab's unit, the oc launcher) finds opencode on PATH, and
# a first install puts it in the shell's PATH the way opencode's own installer does.
if [ -n "$OC_FOUND" ] && [ "$OC_FOUND" -ef "$OC_HOME_BIN" ]; then
  case ":$PATH:" in *":$OC_HOME_DIR:"*) ;; *) export PATH="$OC_HOME_DIR:$PATH" ;; esac
  # A shell the user opens later finds it only through a line: one this run installs,
  # and one already here that is on no PATH (a v1.18.3 install skipped the line for a
  # commented-out one, and an update never wrote it after). A commented-out line is no PATH.
  if [ -z "$OC_ON_PATH" ] && ! grep -qsE '^[^#]*\.opencode/bin' "$HOME/.bashrc"; then
    if [ "$OC_WAS_ABSENT" -eq 1 ]; then
      printf '\n# opencode, installed by dgx-spark-qwen38 (%s)\nexport PATH="$HOME/.opencode/bin:$PATH"\n' "$OPENCODE_VERSION" >> "$HOME/.bashrc"
    else
      printf '\n# opencode, put on the PATH by dgx-spark-qwen38\nexport PATH="$HOME/.opencode/bin:$PATH"\n' >> "$HOME/.bashrc"
    fi
    echo "added ~/.opencode/bin to your PATH in ~/.bashrc (open a new shell, or: export PATH=\"\$HOME/.opencode/bin:\$PATH\")"
  fi
fi
# A complete, ready-to-use opencode config (https://opencode.ai). The limits
# satisfy the serving window in BOTH modes, including when opencode's hidden
# 32000 output cap is lifted by the oc launcher below. The engine refuses any
# request whose prompt + max_tokens passes its window, and the prompt opencode
# sends can reach its compaction threshold plus one worst step (oc-limits.sh):
#   native: 173000 - 20000 + 43863 + 64000 = 260863 <= 262144
#   1m:     700000 (compaction at 680000) + 200000 = 880000, against the four
#           pools measured on the official image at 0.76 (2026-09-17 and 18:
#           902,398 / 889,131 / 889,722 / 887,797). The margin over the worst of
#           those is 7,797 tokens, under 1%, and this box has reported 863,398
#           in an earlier campaign, where the pair does not fit at all. These
#           static numbers are the starting point, not the contract: step 9
#           runs oc-fit-limits.py against the pool the boot actually got and
#           rewrites them (551,000 + 183,000 there). A box that skips that fit
#           (--no-opencode, or the NOTE path when the engine is not up) keeps
#           the static pair and can meet a proxy 400 late in a session.
# Service installs point agent clients at the keepalive proxy (step 8): SGLang
# buffers tool-call arguments at any context length and agent CLIs abort
# silent streams. --no-service has no proxy: direct server port for ./run.sh.
# The key is referenced via {file:...}: no secret in the file.
# opencode limits per context mode: one table, in oc-limits.sh, because
# switch-model.sh has to write the same numbers and a second copy is how the two
# drift. See that script for why the numbers matter (a limit above what the lane
# serves turns a compaction into a 400 mid-session).
if [ "${LANE:-27b}" = "flash" ]; then
  OC_SELECTOR="$FLASH_TIER"
else
  OC_SELECTOR="$CONTEXT_MODE"
fi
# captured first: `read <<<"$(cmd)"` returns 0 whatever cmd returned, so this refusal
# never fired and a refused target died below under "returned no limits"
OC_ROW="$("$REPO_DIR/oc-limits.sh" "$MODEL_CHOICE" "$OC_SELECTOR")" \
  || die "oc-limits.sh refused MODEL_CHOICE=$MODEL_CHOICE with $OC_SELECTOR (repo bug: please open an issue)"
read -r OC_CTX OC_OUT OC_LABEL <<<"$OC_ROW"
[ -n "${OC_CTX:-}" ] && [ -n "${OC_OUT:-}" ] \
  || die "oc-limits.sh returned no limits for $MODEL_CHOICE/$OC_SELECTOR"
# The output CEILING is not this target's number: opencode sends
# max_tokens = min(limit.output, the ceiling), limit.output is what a switch
# rewrites, and a ceiling taken from the installed target survives the switch
# and cuts the next lane's turn in silence. So it is the table's maximum.
OC_OUT_CAP="$("$REPO_DIR/oc-limits.sh" --max-out)" \
  || die "oc-limits.sh --max-out failed (repo bug: please open an issue)"
[ "${OC_OUT_CAP:-0}" -ge "$OC_OUT" ] \
  || die "oc-limits.sh --max-out returned $OC_OUT_CAP, below this target's $OC_OUT"
OC_PORT="$PROXY_PORT"
[ "${NO_SERVICE:-0}" -eq 1 ] && OC_PORT="$PORT"
# end oc mode
# A provider appears in the picker only when its engine is installed on this
# box (nobody should be offered a model that nothing serves); the model being
# installed right now always appears. The default model is the one being
# installed. json.dump writes the file: no hand-escaping.
OC_27B=0; OC_FLASH=0
{ [ "$LANE" = "27b" ] || [ -f "$SGL_UNIT_PATH" ]; } && OC_27B=1
{ [ "$LANE" = "flash" ] || [ -f "$FLASH_UNIT_PATH" ]; } && OC_FLASH=1
# How much of a conversation survives a compaction verbatim, from the same table.
OC_KEEP="$("$REPO_DIR/oc-limits.sh" --preserve "$OC_CTX")" \
  || die "oc-limits.sh --preserve failed for $OC_CTX (repo bug: please open an issue)"
# The other engine's limits, for when both providers are present: the 27B block
# keeps its context-mode limits, flash always serves its native window. A flash install
# runs in native mode, so the 27B's own mode comes from its unit: taken from this run, a
# 1M 27B got the native 194048/64000, which oc and the Agent tab read first since they
# load this file over the user's (reference box, 2026-09-23).
OC_27B_MODE="$CONTEXT_MODE"
if [ "$LANE" = "flash" ]; then
  OC_27B_MODE=native
  if grep -qs -- '--context-length 1010000' "$SGL_UNIT_PATH"; then OC_27B_MODE=1m; fi
fi
# Its pair comes from the same table, for the checkpoint that unit serves: FP8 has its
# own 1M pair, and a copy of the numbers here had drifted from the table (it gave an FP8
# box 700000/200000; found in review, 2026-09-24).
OC_27B_CHOICE=stock
case "$(grep -oE -- '--model-path [^ ]+' "$SGL_UNIT_PATH" 2>/dev/null | head -1 | cut -d' ' -f2 || true)" in
  "$FP8_REPO"|"$UNCFP8_REPO") OC_27B_CHOICE=fp8 ;;
esac
OC_27B_PAIR="$("$REPO_DIR/oc-limits.sh" "$OC_27B_CHOICE" "$OC_27B_MODE")" \
  || die "oc-limits.sh refused $OC_27B_CHOICE/$OC_27B_MODE (repo bug: please open an issue)"
OC_27B_CTX="${OC_27B_PAIR%% *}"; OC_27B_OUT="$(echo "$OC_27B_PAIR" | cut -d' ' -f2)"
OC_LANE="$LANE" OC_27B="$OC_27B" OC_FLASH="$OC_FLASH" OC_PORT="$OC_PORT" \
OC_CTX="$OC_CTX" OC_OUT="$OC_OUT" OC_LABEL="$OC_LABEL" OC_CONTEXT_MODE="$OC_27B_MODE" \
OC_27B_CTX="$OC_27B_CTX" OC_27B_OUT="$OC_27B_OUT" \
OC_KEEP="$OC_KEEP" OC_PIN="$OPENCODE_PIN" \
OC_CONFIG_DIR="$CONFIG_DIR" python3 - <<'PYEOF' || die "could not write the opencode provider config"
import json
import os

cfg_dir = os.environ["OC_CONFIG_DIR"]
lane = os.environ["OC_LANE"]
# "lean" first: it is the level the patched template defaults to, so a picker
# that lists it first shows the level a client gets when it selects nothing.
# The three Qwen levels stay, unchanged, for anyone who wants them by name.
variants = {lvl: {"chat_template_kwargs": {"reasoning_effort": lvl}}
            for lvl in ("lean", "low", "medium", "xhigh")}
key_ref = f"{{file:{cfg_dir}/api-key}}"
base_url = f"http://127.0.0.1:{os.environ['OC_PORT']}/v1"

def prov(name, model_id, model_name, ctx, out):
    return {
        "npm": "@ai-sdk/openai-compatible",
        "name": name,
        "options": {"baseURL": base_url, "apiKey": key_ref},
        "models": {model_id: {
            "name": model_name,
            "limit": {"context": ctx, "input": ctx, "output": out},
            "variants": variants,
            "attachment": True,
            "modalities": {"input": ["text", "image"], "output": ["text"]},
        }},
    }

def fitted(provider, model_id, bound_ctx, bound_out):
    """The pair a fit to the engine's pool left here under the table's 1m bounds, if any.
    On a 1m box the table only gives bounds and the fit (end of the install, or the
    cockpit) sets the pair: writing the bounds over it made every 1m run write twice, the
    bounds here and the fit at the end, with an opencode restart for each (found in
    review, 2026-09-24)."""
    try:
        with open(f"{cfg_dir}/opencode.json") as f:
            lim = json.load(f)["provider"][provider]["models"][model_id]["limit"]
        c, o = int(lim["context"]), int(lim["output"])
    except Exception:
        return None
    return (c, o) if 0 < c <= bound_ctx and 0 < o <= bound_out else None

providers = {}
if os.environ["OC_27B"] == "1":
    if lane == "27b":
        ctx, out, label = int(os.environ["OC_CTX"]), int(os.environ["OC_OUT"]), os.environ["OC_LABEL"]
    else:  # flash install on a box that also has the 27B unit: keep its own limits
        ctx, out = int(os.environ["OC_27B_CTX"]), int(os.environ["OC_27B_OUT"])
        label = "local, 1M" if os.environ["OC_CONTEXT_MODE"] == "1m" else "local"
    if os.environ["OC_CONTEXT_MODE"] == "1m":
        ctx, out = fitted("qwen38", "qwen3.8-27b", ctx, out) or (ctx, out)
    providers["qwen38"] = prov("Qwen3.8-27B (DGX Spark)", "qwen3.8-27b",
                               f"Qwen3.8-27B NVFP4+DFlash2 ({label})", ctx, out)
if os.environ["OC_FLASH"] == "1":
    # the flash lane's limits follow the pool math above (OC_CTX/OC_OUT); a 27B
    # install that also lists flash gets the same pool-safe constants
    fctx, fout = (int(os.environ["OC_CTX"]), int(os.environ["OC_OUT"])) if lane == "flash" else (110000, 32000)
    providers["flashnext"] = prov("Qwen3.8-Flash-Next (DGX Spark)", "qwen3.8-flash-next",
                                  "Qwen3.8-Flash-Next NVFP4+MTP (local, 262K)", fctx, fout)

default = "flashnext/qwen3.8-flash-next" if lane == "flash" else "qwen38/qwen3.8-27b"
# Compaction is global in opencode.json, not per-model, so it is sized from the
# INSTALLED target (oc-limits.sh --preserve) and rewritten by every install and
# switch. preserve_recent_tokens exists because opencode's own default clamps it
# at 15,000 whatever the window: on a 262K lane a compaction at 205,000 tokens
# would keep 15,000 and summarise the other 190,000, which is not using the
# window, it is refilling it from scratch. prune lets opencode clear stale tool
# output (never the last two turns, never the most recent 40,000 tokens of it)
# instead of summarising everything, which is the cheaper way to stay under the
# ceiling on an agent lane that reads a lot of files.
doc = {"$schema": "https://opencode.ai/config.json", "provider": providers,
       "compaction": {"preserve_recent_tokens": int(os.environ["OC_KEEP"]), "prune": True},
       "model": default, "small_model": default}
# With the version pinned, opencode announces a release instead of installing it itself
# (unset, it installs its own patch releases; see the OPENCODE_VERSION pin).
if os.environ.get("OC_PIN") == "1":
    doc["autoupdate"] = "notify"
# written only when it changes, so its date says when it last did: opencode-web is
# restarted below only for a config newer than it
text = json.dumps(doc, indent=2) + "\n"
try:
    with open(f"{cfg_dir}/opencode.json") as f:
        same = f.read() == text
except OSError:
    same = False
if not same:
    with open(f"{cfg_dir}/opencode.json", "w") as f:
        f.write(text)
print(f"{'kept' if same else 'wrote'} {cfg_dir}/opencode.json (default {default}, "
      f"providers: {', '.join(providers) or 'none'})")
PYEOF
echo "opencode limits: context $OC_CTX, output $OC_OUT, port $OC_PORT"
echo "  compaction fires at $((OC_CTX - 20000)) tokens and keeps $OC_KEEP verbatim"
OC_USER_CFG="$HOME/.config/opencode/opencode.json"
# A box with no opencode config of its own gets this one. There is nothing to merge
# into and nothing of the user's to keep, and nothing else points opencode at the file
# above: without this, a first install left opencode, oc and the Agent tab with no
# provider for the model the box serves, and a printed cp command to find.
if [ ! -e "$OC_USER_CFG" ]; then
  mkdir -p "$HOME/.config/opencode"
  install -m 644 "$CONFIG_DIR/opencode.json" "$OC_USER_CFG"
  echo "  no opencode config yet: installed this repo's at $OC_USER_CFG"
elif ! grep -qsE '"(qwen38|flashnext)"' "$OC_USER_CFG"; then
  # the user's own config, with none of this repo's providers in it: theirs to merge
  echo "  your $OC_USER_CFG has no provider for this box: merge the \"qwen38\" (or \"flashnext\")"
  echo "  block from $CONFIG_DIR/opencode.json into it (docs/opencode.md)"
else
  # A config set up for this box, by the copy above or by hand, lists the lanes the
  # box had then. A lane installed since (the 27B first, the flash lane later) brings
  # its provider in: without it the merges below find nothing to merge, and opencode
  # sends the other lane's limits and label to the one that serves.
  python3 "$REPO_DIR/oc-merge-limits.py" "$OC_USER_CFG" --add-providers "$CONFIG_DIR/opencode.json" || true
fi
# An existing opencode.json keeps the user's other providers, but its limits for
# THIS lane must follow the served engine (v1.5.2: a flash conversation allowed
# to grow to 226000 tokens outgrew the 159k KV pool and wedged the scheduler).
if [ -f "$OC_USER_CFG" ] && [ "$OPENCODE_PIN" = "1" ]; then
  python3 "$REPO_DIR/oc-merge-limits.py" "$OC_USER_CFG" --autoupdate notify || true
fi
if [ -f "$OC_USER_CFG" ]; then
  if [ "${LANE:-27b}" = "flash" ]; then
    python3 "$REPO_DIR/oc-merge-limits.py" "$OC_USER_CFG" flashnext qwen3.8-flash-next "$OC_CTX" "$OC_OUT" || true
    python3 "$REPO_DIR/oc-merge-limits.py" "$OC_USER_CFG" --compaction "$OC_KEEP" || true
    # The lean level exists in this lane's template because the run above just
    # patched it. It is offered only for the lane being installed: a provider
    # entry advertising a level the served template does not know answers 500.
    python3 "$REPO_DIR/oc-merge-limits.py" "$OC_USER_CFG" flashnext qwen3.8-flash-next --add-variant lean || true
  else
    # Never downgrade a 27B unit that serves a larger window than this run's
    # CONTEXT_MODE computed (a native-mode re-install clobbered a 1m user's
    # 700000/200000 back to 194048/64000, reference box 2026-08-30). Only a mode
    # nobody asked for, though: an explicit CONTEXT_MODE=native installs the native
    # unit at step 8, and the 1M limits kept here then asked 700,000 of a 262,144
    # window, a 400 past it, under advice to pass CONTEXT_MODE=1m (found in review,
    # 2026-09-24). Since the mode converges on the installed unit, an explicit one is
    # the only way here.
    UNIT_CTX="$(grep -oE -- '--context-length [0-9]+' "$SGL_UNIT_PATH" 2>/dev/null | awk '{print $2}' | head -1 || true)"
    if [ -n "$UNIT_CTX" ] && [ "$UNIT_CTX" -gt 262144 ] && [ "$CONTEXT_MODE" != "1m" ] && [ -z "$_ENV_CONTEXT_MODE" ]; then
      echo "NOTE: the installed 27B unit serves --context-length $UNIT_CTX; keeping the existing"
      echo "      opencode limits (these $CONTEXT_MODE-mode values would shrink them). Re-run with"
      echo "      CONTEXT_MODE=1m to manage them, or edit $OC_USER_CFG yourself."
    else
      # on 1m the table's pair is a bound, and the fit at the end sets the pair: keep one
      # already under it (see oc-merge-limits.py --keep-lower)
      OC_KEEP_LOWER=(); [ "$CONTEXT_MODE" = "1m" ] && OC_KEEP_LOWER=(--keep-lower)
      python3 "$REPO_DIR/oc-merge-limits.py" "$OC_USER_CFG" qwen38 qwen3.8-27b "$OC_CTX" "$OC_OUT" ${OC_KEEP_LOWER[@]+"${OC_KEEP_LOWER[@]}"} || true
      python3 "$REPO_DIR/oc-merge-limits.py" "$OC_USER_CFG" --compaction "$OC_KEEP" || true
    fi
    # Offered whatever the limits branch decided above: the level comes from the
    # template this run patched, not from the context mode.
    python3 "$REPO_DIR/oc-merge-limits.py" "$OC_USER_CFG" qwen38 qwen3.8-27b --add-variant lean || true
  fi
fi
# The default model and the served entry's picker name follow the install, not
# just the limits: an install that changes lane left opencode offering and
# defaulting to the previous lane (reference box 2026-09-11: flash selected
# while stock 1M served). Same helper as switch-model.sh, one label table.
OC_WINDOW=262144; [ "$CONTEXT_MODE" = "1m" ] && OC_WINDOW=1010000
python3 "$REPO_DIR/oc-point-default.py" "$CONFIG_DIR/opencode.json" "$LANE" "$MODEL_CHOICE" "$OC_WINDOW" \
  || die "could not point the generated opencode config at the installed lane"
if [ -f "$OC_USER_CFG" ]; then
  python3 "$REPO_DIR/oc-point-default.py" "$OC_USER_CFG" "$LANE" "$MODEL_CHOICE" "$OC_WINDOW" || true
fi
# Every opencode.json this install touches has been written by now, except the limits
# a 1m install fits to the pool once the engine is up (step 9, which restarts it again),
# so the server that reads them can be restarted. It parses opencode.json ONCE at startup and
# never again (measured 2026-09-13: the file said 225,000 while the running
# server still answered 175,000 on /config), so without this the Agent tab keeps
# compacting against the previous install's window. Last, not mid-write: a
# restart before the user's own config is merged would reload the old numbers.
# Only when one of them changed: this restart ended whatever turn the Agent tab was in
# the middle of, on every run, a run that changed nothing included (up to three restarts
# of opencode-web per install on 2026-09-23; found in review, 2026-09-24).
if systemctl list-unit-files opencode-web.service >/dev/null 2>&1 \
   && systemctl is-active --quiet opencode-web.service; then
  if [ "$(oc_configs_sum)" != "$OC_SUM_BEFORE" ]; then
    sudo systemctl restart opencode-web.service \
      && echo "opencode-web.service restarted so it reads the new limits" \
      || echo "NOTE: restart opencode-web.service by hand, or the Agent tab keeps the old limits"
  else
    echo "opencode-web.service kept: the configs it reads did not change"
  fi
fi
# oc: launcher that lifts opencode's hidden 32000 max_tokens cap to the
# declared output limit (without it, long thinking is cut at 32000 and the
# turn ends silently). Never clobbers a foreign oc binary (e.g. OpenShift).
OC_BIN="$HOME/.local/bin/oc"
OC_EXISTING="$(command -v oc || true)"
# Not ours: a file at the launcher's own path without this repo's mark (OpenShift's oc
# lives there too), or another oc first on the PATH. Only the second was looked at, so an
# oc at the very path was overwritten, and so was one in a ~/.local/bin this shell's PATH
# lacks (found in review, 2026-09-24).
OC_FOREIGN=""
if [ -e "$OC_BIN" ] && ! grep -q 'dgx-spark-qwen38' "$OC_BIN" 2>/dev/null; then
  OC_FOREIGN="$OC_BIN"
elif [ -n "$OC_EXISTING" ] && [ "$OC_EXISTING" != "$OC_BIN" ] && ! grep -q 'dgx-spark-qwen38' "$OC_EXISTING" 2>/dev/null; then
  OC_FOREIGN="$OC_EXISTING"
fi
if [ -n "$OC_FOREIGN" ]; then
  echo "NOTE: an unrelated 'oc' command exists at $OC_FOREIGN; not installing the launcher."
  # OPENCODE_CONFIG as the launcher sets it: without it opencode sends the prompts to its
  # own hosted model when the global config names no provider for this box (2026-09-23)
  echo "      Launch opencode with:  OPENCODE_CONFIG=$CONFIG_DIR/opencode.json OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX=$OC_OUT_CAP opencode --yolo"
else
  mkdir -p "$HOME/.local/bin"
  cat > "$OC_BIN" <<OCWRAP
#!/bin/bash
# oc launcher installed by dgx-spark-qwen38: opencode wired to the local server.
# Lifts opencode's hidden 32000 max_tokens cap above every output limit this
# repo declares; without this, long thinking phases are cut at 32000 and the
# turn ends silently. It is a ceiling: the per-model limit.output in
# opencode.json is the number that follows a model switch, and this one only has
# to stay above it, which is why it is not the installed target's own limit.
# --yolo auto-approves permissions (the reference box runs this way; remove it
# below if you prefer per-action prompts).
export OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX="\${OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX:-$OC_OUT_CAP}"
# The box's providers and default model, over whatever the global opencode config
# says (opencode loads OPENCODE_CONFIG after it). Without this, a config with no
# provider for this box sent oc's prompts to opencode's own hosted model (2026-09-23).
[ -f "$CONFIG_DIR/opencode.json" ] && export OPENCODE_CONFIG="\${OPENCODE_CONFIG:-$CONFIG_DIR/opencode.json}"
OPENCODE_BIN="\$(command -v opencode || true)"
[ -n "\$OPENCODE_BIN" ] || OPENCODE_BIN="\$HOME/.opencode/bin/opencode"
if [ ! -x "\$OPENCODE_BIN" ]; then
  echo "oc: opencode is not installed. Re-run ./install.sh in $REPO_DIR: it installs the opencode this repo tests ($OPENCODE_VERSION)." >&2
  exit 127
fi
# --yolo goes LAST: opencode's parser rejects global flags before a
# subcommand (opencode --yolo run ... prints the help instead of running)
exec "\$OPENCODE_BIN" "\$@" --yolo
OCWRAP
  chmod +x "$OC_BIN"
  echo "installed the oc launcher at $OC_BIN (output ceiling $OC_OUT_CAP, this target's limit $OC_OUT)"
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) echo "NOTE: $HOME/.local/bin is not in this shell's PATH: a new login has it (Ubuntu's ~/.profile"
       echo "      adds it once the folder exists), or call $OC_BIN directly." ;;
  esac
fi
fi
# end of step 7

if [ "$NO_SERVICE" -eq 1 ]; then
  apply_context_configs
  printf '\n\033[1;32m✅ Prepared (no systemd, nothing needed sudo).\033[0m\n'
  echo "  Run in the foreground: ./run.sh     (Ctrl+C stops it; first boot ≈ 9 min)"
  # Not just two folders: the summary said so, and left out the serving image, opencode,
  # the oc launcher and the provider this box adds to the user's opencode config, which
  # reads the key in the config folder: opencode refuses to start while it points at a key
  # that is gone (found in review, 2026-09-24).
  echo "  To remove it later: ./uninstall.sh --list names everything this left and changes nothing:"
  echo "  $CONFIG_DIR (the key, templates), the checkpoints in $HF_CACHE, the serving image, opencode"
  echo "  and its oc launcher, and a provider in your opencode config that reads $CONFIG_DIR/api-key."
  echo "  Remove that provider with the key, or opencode refuses to start."
  echo "  No cockpit either: it is a systemd unit, and --no-service installs none."
  exit 0
fi

step "8/10 Installing the systemd service (sudo needed)"
# The steps below make most of this script's sudo calls, and a dead timestamp used to
# kill the install here with no message at all (set -e on a bare `sudo cp`, reference
# box 2026-09-13, three times in one afternoon: background runs and passwordless
# contexts have no tty for sudo to ask on). The ticket asked for at step 1 can have
# expired during the downloads (sudo keeps one 15 min), so it is renewed here: with a
# terminal sudo asks again, without one `sudo -v` fails at once and the refusal names
# the fix. `sudo -n` alone never asked, and a fresh one-liner died here after ~20 min.
sudo -n true 2>/dev/null || sudo -v || die "sudo needs a fresh timestamp before the systemd step: run 'sudo -v', then re-run ./install.sh (completed steps are skipped)"
UNIT_PATH="/etc/systemd/system/$UNIT_NAME"
if [ -f "$UNIT_PATH" ]; then
  # Safety net for hand-tuned units: the previous unit stays recoverable.
  sudo cp "$UNIT_PATH" "$CONFIG_DIR/$UNIT_NAME.bak-preupdate"
  sudo chown "$(id -u):$(id -g)" "$CONFIG_DIR/$UNIT_NAME.bak-preupdate"
  echo "previous unit backed up at $CONFIG_DIR/$UNIT_NAME.bak-preupdate"
fi
if [ "$LANE" = "flash" ]; then
  UNIT_TPL="$REPO_DIR/qwen38-flash.service.template"
  SERVE_IMAGE_FINAL="$FLASH_SERVE_IMAGE"
else
  UNIT_TPL="$REPO_DIR/qwen38-sglang.service.template"
  [ "$CONTEXT_MODE" = "1m" ] && UNIT_TPL="$REPO_DIR/qwen38-sglang-1m.service.template"
  SERVE_IMAGE_FINAL="$SERVE_IMAGE"
fi
# Serve-time revision lock: the pinned sha is passed to the server itself, so
# an upstream push to the checkpoint repo can never change what is served
# (download-time pinning alone leaves "main" resolvable at boot). A kept
# custom model reuses its unit's existing --revision, or none.
if [ "$KEEP_MODEL_VERBATIM" -eq 1 ]; then
  MODEL_REV_ARGS=""
  [ -n "${CUR_REV:-}" ] && MODEL_REV_ARGS="--revision $CUR_REV"
else
  MODEL_REV_ARGS="--revision $MODEL_REV"
fi
# The 27B lane serves with an fp8 KV cache. The NVFP4 checkpoints carry their own
# KV scales so SGLang picks it up from the checkpoint; Qwen's FP8 release does not,
# and defaults to a bf16 KV cache that costs about half the pool (measured on the
# reference box, same 1m unit: 771,139 tokens with the flag, 382,706 without). So
# the FP8 pair asks for it explicitly, which is what the reference box has served
# since 2026-08-31 and what every FP8 pool number in this repo was measured on.
case "$MODEL_CHOICE" in
  fp8|uncensored-fp8) KV_CACHE_ARGS="--kv-cache-dtype fp8_e4m3 " ;;
  *)                  KV_CACHE_ARGS="" ;;
esac

# The reduced draft vocabulary is a speculative-path flag: a tier that does not
# speculate must not receive it. Rendered as a whole line so an off setting
# leaves no dangling backslash in the launcher.
SPEC_TOKEN_MAP_LINE=""
case "$FLASH_TIER_ARGS" in
  *--speculative-algorithm*)
    if [ "${SPEC_TOKEN_MAP_SIZE:-0}" -gt 0 ] && [ -s "$CONFIG_DIR/$TOKEN_MAP_NAME" ]; then
      SPEC_TOKEN_MAP_LINE="TIER+=(--speculative-token-map /out/$TOKEN_MAP_NAME)"
    fi ;;
esac

# Whatever the convergence block decided, the per-checkpoint and per-tier args
# are derived from it here, once, before anything is rendered.
resolve_flash_checkpoint_args
resolve_flash_tier_args
# And a flash launcher without its checkpoint's own flags is a ten-minute boot
# that dies at MoE autotune, so it never gets written.
if [ "$LANE" = "flash" ] && [ -z "$FLASH_QUANT_ARGS" ]; then
  die "internal error: the flash lane resolved no checkpoint flags for MODEL_CHOICE=$MODEL_CHOICE. Refusing to write a launcher that would fail ten minutes into its boot. Please open an issue with this line."
fi

render_tpl() {  # $1 template file; substituted result on stdout
  # Values are paths and versions, and paths may carry sed-special bytes
  # (& expands to the match, | is the delimiter, backslash escapes): escape
  # once so a mount point like /mnt/a&b can never corrupt an installed unit.
  esc() { printf '%s' "$1" | sed -e 's/[\\/&|]/\\&/g'; }
  sed -e "s|__HOME__|$(esc "$HOME")|g" \
      -e "s|__USER__|$(esc "$(id -un)")|g" \
      -e "s|__GROUP__|$(esc "$(id -gn)")|g" \
      -e "s|__PORT__|$(esc "$PORT")|g" \
      -e "s|__ENGINE_BIND__|$(esc "$ENGINE_BIND")|g" \
      -e "s|__IMAGE__|$(esc "$SERVE_IMAGE_FINAL")|g" \
      -e "s|__HF_CACHE__|$(esc "$HF_CACHE")|g" \
      -e "s|__DRAFT2_REV__|$(esc "$DRAFT2_REV")|g" \
      -e "s|__DRAFT2_REPO__|$(esc "$DRAFT2_REPO")|g" \
      -e "s|__DRAFT2_QUANT__|$(esc "$DRAFT2_QUANT")|g" \
      -e "s|__DRAFT2_TOKENS__|$(esc "$DRAFT2_TOKENS")|g" \
      -e "s|__MODEL_REV_ARGS__|$(esc "$MODEL_REV_ARGS")|g" \
      -e "s|__MODEL__|$(esc "$MODEL_REPO")|g" \
      -e "s|__PLE_DIR__|$(esc "$PLE_DIR")|g" \
      -e "s|__MODEL_REV__|$(esc "$MODEL_REV")|g" \
      -e "s|__KV_CACHE_ARGS__|$(esc "$KV_CACHE_ARGS")|g" \
      -e "s|__FLASH_TIER_ARGS__|$(esc "$FLASH_TIER_ARGS")|g" \
      -e "s|__FLASH_QUANT_ARGS__|$(esc "$FLASH_QUANT_ARGS")|g" \
      -e "s|__FLASH_MEM_FRACTION__|$(esc "$FLASH_MEM_FRACTION")|g" \
      -e "s|__PLE_RSS_BUDGET_GB__|$(esc "$PLE_RSS_BUDGET_GB")|g" \
      -e "s|__SPEC_TOKEN_MAP_LINE__|$(esc "$SPEC_TOKEN_MAP_LINE")|g" \
      "$1"
}
if [ "$LANE" = "flash" ]; then
  mkdir -p "$PLE_DIR"
  # The docker-run command lives in a plain launch script, not in ExecStart:
  # systemd applies its own C-style unescaping before bash would, and the JSON
  # arguments (--speculative-config, splitting_ops) do not survive two rounds.
  TMP_LAUNCH="$(mktemp)"
  render_tpl "$REPO_DIR/qwen38-flash-launch.sh.template" > "$TMP_LAUNCH"
  bash -n "$TMP_LAUNCH" || die "rendered flash launch script does not parse (report this repo bug)"
  # rewritten only when it changes: the engine reads it at start, and a rewrite, even an
  # identical one, would make the next run restart a running engine (engine-inputs.py)
  cmp -s "$TMP_LAUNCH" "$CONFIG_DIR/launch-flash.sh" || install -m 755 "$TMP_LAUNCH" "$CONFIG_DIR/launch-flash.sh"
  rm -f "$TMP_LAUNCH"
  echo "wrote $CONFIG_DIR/launch-flash.sh"
fi
TMP_UNIT="$(mktemp)"
render_tpl "$UNIT_TPL" > "$TMP_UNIT"
apply_context_configs              # the configs, then at once the unit that reads them
# the same rule for the unit: an identical one is left as it is, mtime included
cmp -s "$TMP_UNIT" "/etc/systemd/system/$UNIT_NAME" || sudo install -m 644 "$TMP_UNIT" "/etc/systemd/system/$UNIT_NAME"
rm -f "$TMP_UNIT"
# Cross-engine switch: exactly one serving unit may start at boot. The other
# engine's unit (if present) is disabled now and stopped at step 9, right
# before this one starts; its unit file is kept for a fast switch back.
OTHER_UNIT=""
if [ "$LANE" = "flash" ]; then
  [ -f "$SGL_UNIT_PATH" ] && OTHER_UNIT="qwen38-sglang.service"
else
  [ -f "$FLASH_UNIT_PATH" ] && OTHER_UNIT="qwen38-flash.service"
fi
if [ -n "$OTHER_UNIT" ] && systemctl is-enabled --quiet "$OTHER_UNIT" 2>/dev/null; then
  echo "disabling the other engine's unit at boot: $OTHER_UNIT (file kept, switch back anytime with ./switch-model.sh)"
  sudo systemctl disable "$OTHER_UNIT"
fi
# The image lane is the third engine. When this run makes a text lane the boot lane (an
# explicit MODEL_CHOICE on a box that booted images), leaving it enabled would put two
# engines at the next boot.
if [ "$IMAGE_BOOT" -eq 0 ] && systemctl is-enabled --quiet qwen38-image.service 2>/dev/null; then
  echo "disabling the image lane at boot (unit kept, switch back anytime from the cockpit)"
  sudo systemctl disable qwen38-image.service
fi
KEEPALIVE_UNIT="qwen38-keepalive.service"
# One-prompt ceiling enforced by the proxy (tokens; 0 = pool share only). The proxy
# always refuses a prompt above its share of the KV pool as well, so the smaller of
# the two binds and a tier with a small pool needs no separate ceiling.
#
# Flash lane, 128,000 from v1.5.6 to v1.7 and 200,000 from v1.8: the prefill of a
# long prompt grew the engine's memory by ~0.27 GiB per 1k tokens beyond ~90k
# (measured 29/08, a 120k prompt cost ~9 GiB of host headroom), which put a 150k
# prompt at the memory edge. That growth was the PLE table's mapping faulting in
# whole page-cache folios, and v1.8 serves an engine that trims it.
#
# How far it trims was only half measured: 200,058 tokens cost 3.5 GiB, and above
# that was left deliberately unmeasured because a livelock on this box costs a
# power cycle. Measured on 2026-09-13, MemAvailable sampled through each prefill,
# engine up 19 h with the PLE budget already filled:
#
#     195,784 tokens -> 1.16 GiB of host headroom,  94.7 s
#     225,051 tokens -> 1.57 GiB,                  111.3 s
#     249,500 tokens -> 1.52 GiB,                  126.1 s, floor 7.0 GiB
#
# The cost is flat past 200k and an order of magnitude below what the v1.5 engine
# paid, so memory is no longer what caps this lane. What caps it is the engine's
# own wall, max_req_input_len = 262,138: the ceiling is 250,000, which leaves
# 12,138 tokens of slack under it. That band matters because until 2026-09-13 the
# launcher passed --allow-auto-truncate and a prompt over the wall came back
# silently cut rather than refused.
# The flash lane's ceiling goes in the unit whatever lane this installs: the proxy applies
# it while the flash lane serves (v6.25), so a switch no longer has to move it with a
# restart. PROMPT_CEILING_TOKENS, when given, is a ceiling on any lane.
PROMPT_CEILING="${PROMPT_CEILING_TOKENS:-0}"
FLASH_PROMPT_CEILING="${FLASH_PROMPT_CEILING_TOKENS:-250000}"
[[ "$PROMPT_CEILING$FLASH_PROMPT_CEILING" =~ ^[0-9]+$ ]] || die "PROMPT_CEILING_TOKENS and FLASH_PROMPT_CEILING_TOKENS take a number of tokens"
# Every service install gets the keepalive proxy: SGLang buffers tool-call
# arguments while they stream (127 s of measured silence on a 400-line write,
# at native context) and agent CLIs abort a silent stream (~140-180 s for
# opencode). It also aborts zombie generations when the client disconnects.
KA_CHANGED=0      # whether this run changed what the proxy runs (see its restart below)
cmp -s "$REPO_DIR/keepalive-proxy.py" "$CONFIG_DIR/keepalive-proxy.py" \
  || { install -m 755 "$REPO_DIR/keepalive-proxy.py" "$CONFIG_DIR/keepalive-proxy.py"; KA_CHANGED=1; }
TMP_KA="$(mktemp)"
sed -e "s|__HOME__|$HOME|g" \
    -e "s|__USER__|$(id -un)|g" \
    -e "s|__GROUP__|$(id -gn)|g" \
    -e "s|__PORT__|$PORT|g" \
    -e "s|__PROXY_PORT__|$PROXY_PORT|g" \
    -e "s|__PROXY_BIND__|$PROXY_BIND|g" \
    -e "s|__PROMPT_CEILING__|$PROMPT_CEILING|g" \
    -e "s|__FLASH_PROMPT_CEILING__|$FLASH_PROMPT_CEILING|g" \
    "$REPO_DIR/qwen38-keepalive.service.template" > "$TMP_KA"
cmp -s "$TMP_KA" "/etc/systemd/system/$KEEPALIVE_UNIT" \
  || { sudo install -m 644 "$TMP_KA" "/etc/systemd/system/$KEEPALIVE_UNIT"; KA_CHANGED=1; }
rm -f "$TMP_KA"
sudo systemctl enable "$KEEPALIVE_UNIT"
# The ceiling lives in the unit this installer writes. A switch-model.sh
# drop-in from an earlier lane would override it silently, so it goes: either
# path converges on the installed lane's ceiling.
if [ -f "/etc/systemd/system/$KEEPALIVE_UNIT.d/ceiling.conf" ]; then
  echo "removing the stale keepalive ceiling drop-in (the installed unit carries the ceiling now)"
  sudo rm -f "/etc/systemd/system/$KEEPALIVE_UNIT.d/ceiling.conf"
  KA_CHANGED=1
  sudo rmdir "/etc/systemd/system/$KEEPALIVE_UNIT.d" 2>/dev/null || true
fi
# The Claude Code warmup was removed in v1.3: clean up what earlier versions
# installed (only the warmup drop-in; any other drop-in in the .d dir is kept).
if [ -f "/etc/systemd/system/$UNIT_NAME.d/warmup.conf" ]; then
  echo "removing the deprecated Claude Code warmup drop-in"
  sudo rm -f "/etc/systemd/system/$UNIT_NAME.d/warmup.conf"
  sudo rmdir "/etc/systemd/system/$UNIT_NAME.d" 2>/dev/null || true
fi
rm -f "$CONFIG_DIR/warmup-claude-code.sh"
sudo systemctl daemon-reload
if [ "$IMAGE_BOOT" -eq 0 ]; then
  sudo systemctl enable "$UNIT_NAME"
else
  # Updated, not switched to. Starting it would stop the image lane that is serving, and
  # the cockpit and the image step below only run once a text engine has proved itself,
  # so this path finishes here, on its own, instead of waiting for a boot it must not do.
  # The proxy's code and unit were just rewritten above; on the text path it is restarted
  # once the engine behind it answers, which this path never waits for. Without this it
  # kept the old keepalive-proxy.py in memory until someone restarted it by hand.
  if [ "$KA_CHANGED" -eq 1 ] || stale_since "$KEEPALIVE_UNIT" "/etc/systemd/system/$KEEPALIVE_UNIT" "$CONFIG_DIR/keepalive-proxy.py"; then
    sudo systemctl restart "$KEEPALIVE_UNIT" \
      || echo "NOTE: could not restart $KEEPALIVE_UNIT; restart it by hand so it runs the new code"
  fi
  if [ "$COCKPIT" -eq 1 ] && [ -x "$REPO_DIR/dashboard/install-dashboard.sh" ]; then
    "$REPO_DIR/dashboard/install-dashboard.sh" \
      || echo "NOTE: the cockpit did not reinstall; retry with ./dashboard/install-dashboard.sh"
  fi
  # --no-smoke always: the smoke test starts and stops the lane, and this lane is serving.
  if [ "$NO_IMAGE" -eq 0 ]; then
    "$REPO_DIR/install-image.sh" --no-smoke \
      || echo "NOTE: the image lane did not update; retry with ./install-image.sh --no-smoke"
  fi
  step "Done: the text lane is up to date; the image lane stays this box's serving lane"
  echo "  text lane : installed as $UNIT_NAME, not enabled at boot"
  if [ "$MODEL_CHOICE" = "custom" ]; then
    # a kept custom model has no switch target of its own; the installer is the way back
    echo "  back to it: MODEL_CHOICE=<target> ./install.sh (this box serves a custom model)"
  else
    echo "  back to it: the cockpit's switcher, or ./switch-model.sh $MODEL_CHOICE"
  fi
  exit 0
fi

if [ "$NO_START" -eq 1 ]; then
  step "Done (service installed and enabled at boot; start it with: sudo systemctl start $UNIT_NAME)"
  if [ -n "$OTHER_UNIT" ] && systemctl is-active --quiet "$OTHER_UNIT" 2>/dev/null; then
    echo "NOTE: $OTHER_UNIT is still serving; stop it first (one engine at a time):"
    echo "      sudo systemctl stop $OTHER_UNIT && sudo systemctl start $UNIT_NAME"
  fi
  echo "also start the keepalive proxy with: sudo systemctl start $KEEPALIVE_UNIT"
  [ "$COCKPIT" -eq 1 ] && echo "the cockpit is not installed on the --no-start path: ./dashboard/install-dashboard.sh once the engine runs"
  exit 0
fi

# Kept only when the running engine started after every file it reads was last written,
# this run changed none of them by content (engine-inputs.py), and it answers.
ENGINE_KEEP=0; ENGINE_WHY=""
if [ "${RESTART_ENGINE:-0}" = "1" ]; then
  ENGINE_WHY="RESTART_ENGINE=1"
elif [ "$ENGINE_RUNNING_IT" != "yes" ]; then
  ENGINE_WHY="${ENGINE_RUNNING_IT#no: }"
elif [ -z "$ENGINE_FP_BEFORE" ] || [ "$(python3 "$REPO_DIR/engine-inputs.py" fingerprint "$ENGINE_UNIT_PATH" "$CONFIG_DIR" "$HF_CACHE" "${ENGINE_CKPTS[@]}" 2>/dev/null || true)" != "$ENGINE_FP_BEFORE" ]; then
  ENGINE_WHY="this run changed what it reads"
elif ! curl -sf -m 5 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  ENGINE_WHY="it does not answer /health"
else
  ENGINE_KEEP=1
fi
if [ "$ENGINE_KEEP" -eq 1 ]; then
  step "9/10 Keeping the running engine: nothing it reads changed since it started (RESTART_ENGINE=1 restarts it)"
elif [ "$LANE" = "flash" ]; then
  step "9/10 Starting (every boot ≈ 12-15 min: the weight load writes the whole 47.7 GiB PLE table into its file, then CUDA graph capture)"
else
  step "9/10 Starting (first boot ≈ 9 min: torch.compile + CUDA graph capture; later boots are faster)"
fi
[ "$ENGINE_KEEP" -eq 0 ] && [ "$ENGINE_RUNNING_IT" != "no: not installed yet" ] && echo "why: $ENGINE_WHY"
if [ -n "$OTHER_UNIT" ] && systemctl is-active --quiet "$OTHER_UNIT" 2>/dev/null; then
  echo "stopping the other engine first ($OTHER_UNIT): one engine at a time on a GB10"
  sudo systemctl stop "$OTHER_UNIT"
fi
SMOKE_MODEL="qwen3.8-27b"
[ "$LANE" = "flash" ] && SMOKE_MODEL="qwen3.8-flash-next"
[ "$ENGINE_KEEP" -eq 1 ] || sudo systemctl restart "$UNIT_NAME"
# A unit that dies at load never reads "failed" here: Restart=always relaunches it after
# RestartSec=15, which never reaches systemd's default limit of 5 starts in 10 s, so
# is-active says "activating" between attempts and the loop below waited its full 20
# minutes with the journal unread. systemd counts the relaunches (NRestarts), and a manual
# restart does not zero the count (checked on the reference box's systemd 255), so the
# count now is the baseline and one more is a crash (found in review, 2026-09-24).
engine_restarts() { systemctl show -p NRestarts --value "$UNIT_NAME" 2>/dev/null | tr -dc '0-9'; }
ENGINE_RESTARTS0="$(engine_restarts)"
# what the wait is made of: the flash lane rewrites its PLE table on every boot, and said
# "first boot compiles kernels" through a 13-minute load that compiled nothing
LOAD_WHY="first boot compiles kernels, be patient"
[ "$LANE" = "flash" ] && LOAD_WHY="every boot writes the 47.7 GiB PLE table, be patient"
for i in $(seq 1 150); do
  if curl -s -m 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    echo "health OK, running a real generation smoke test..."
    # curl on its own, so its failure is named: in the pipe it came out through the ERR trap
    # as "Install failed at line N", and the pointer to the journal below never showed. A
    # kept engine may be in the middle of someone's long prefill, which the smoke request
    # waits behind, so it gets more than a freshly booted one (found in review, 2026-09-24).
    SMOKE_MAX_S=300; [ "$ENGINE_KEEP" -eq 1 ] && SMOKE_MAX_S=1800
    SMOKE_RC=0
    SMOKE_RAW="$(curl -s -m "$SMOKE_MAX_S" "http://127.0.0.1:$PORT/v1/chat/completions" \
      -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
      -d '{"model":"'"$SMOKE_MODEL"'","messages":[{"role":"user","content":"Reply with exactly: READY"}],"max_tokens":600}')" \
      || SMOKE_RC=$?
    [ "$SMOKE_RC" -eq 0 ] || die "Server is up but the smoke generation got no answer (curl exit $SMOKE_RC; 28 is its ${SMOKE_MAX_S} s timeout). Check: journalctl -u $UNIT_NAME -n 50"
    # A wall of "!" (token 0) is this hardware's known decode corruption, and it is not an
    # answer: any non-empty text used to pass, so it ended in "Installed, verified". The
    # smoke calls the engine directly, past the proxy's own tripwire, so it looks itself.
    SMOKE="$(printf '%s' "$SMOKE_RAW" | python3 -c 'import json,sys
try:
    m=json.load(sys.stdin)["choices"][0]["message"]
    text=(m.get("content") or "")+(m.get("reasoning_content") or "")
    print("CORRUPT" if "!"*32 in text else "OK" if text.strip() else "EMPTY")
except Exception as e:
    print(f"FAIL:{e}")')"
    [ "$SMOKE" != "CORRUPT" ] || die "Server is up but the smoke generation came back as a run of '!' (token 0), the decode corruption this hardware is known for, not an answer. Restart the engine (sudo systemctl restart $UNIT_NAME), and if it comes back the same, check: journalctl -u $UNIT_NAME -n 50"
    [ "$SMOKE" = "OK" ] || die "Server is up but the smoke generation failed ($SMOKE). Check: journalctl -u $UNIT_NAME -n 50"
    # A restarted engine can come back with another pool than the one the proxy has cached
    # (it keeps a reading 10 minutes), so the proxy goes with it; with the engine kept, only
    # new code or a new unit is a reason to cut the requests in flight through it.
    if [ "$ENGINE_KEEP" -eq 0 ] || [ "$KA_CHANGED" -eq 1 ] \
       || stale_since "$KEEPALIVE_UNIT" "/etc/systemd/system/$KEEPALIVE_UNIT" "$CONFIG_DIR/keepalive-proxy.py"; then
      sudo systemctl restart "$KEEPALIVE_UNIT"
    else
      echo "keepalive proxy kept: the engine was kept and nothing the proxy runs changed"
    fi
    PROXY_OK=0
    for _ in 1 2 3 4 5; do
      curl -s -m 5 "http://127.0.0.1:$PROXY_PORT/health" >/dev/null 2>&1 && { PROXY_OK=1; break; }
      sleep 2
    done
    [ "$PROXY_OK" = 1 ] || die "the keepalive proxy did not come up on :$PROXY_PORT. Check: journalctl -u $KEEPALIVE_UNIT -n 30"
    echo "keepalive proxy OK on :$PROXY_PORT (agent clients use this port)"
    # Superseded artifacts from earlier versions of THIS repo, if any: detect
    # and say how to reclaim them; never delete data on the operator's behalf.
    LEFTOVER_NOTES=""
    if [ "$LANE" = "flash" ]; then
      for ref in "vllm/vllm-openai:qwen38-flash-next" "vllm/vllm-openai@sha256:fc120ece0a388cc0aa1caad4a9f1cd92113484ab7ec2fd0efadd62585be05bf8" "qwen38-flash:v1.4" "qwen38-flash:v1.5" "qwen38-flash:v1.5.2"; do
        docker image inspect "$ref" >/dev/null 2>&1 && LEFTOVER_NOTES="${LEFTOVER_NOTES}      docker rmi '$ref'\n"
      done
    fi
    while IFS= read -r tagref; do
      # Never offer to delete what is being served, nor the flash overlay tag:
      # since v1.8 the flash overlay image IS the rollback for that lane. The 27B
      # overlay tag is offered, since v1.14 serves the official image instead.
      [ -n "$tagref" ] && [ "$tagref" != "$SERVE_IMAGE" ] && [ "$tagref" != "$FLASH_SERVE_IMAGE" ] \
        && [ "$tagref" != "$OVERLAY_FLASH_SERVE_IMAGE" ] \
        && case "$LEFTOVER_NOTES" in *"'$tagref'"*) ;; *) LEFTOVER_NOTES="${LEFTOVER_NOTES}      docker rmi '$tagref'\n" ;; esac
    done < <({ docker images --format '{{.Repository}}:{{.Tag}}' qwen38-dflash2 2>/dev/null; docker images --format '{{.Repository}}:{{.Tag}}' qwen38-flash 2>/dev/null; } || true)
    if [ -n "$LEFTOVER_NOTES" ]; then
      echo "  Note: earlier versions of this repo left superseded images; reclaim when you like:"
      printf '%b' "$LEFTOVER_NOTES"
      echo "      (full inventory anytime: ./uninstall.sh --list)"
    fi
    if [ "$LANE" = "flash" ] && [ "$OVERLAY_FLASH" != "1" ] \
       && docker image inspect "$OVERLAY_FLASH_SERVE_IMAGE" >/dev/null 2>&1; then
      echo "  Note: $OVERLAY_FLASH_SERVE_IMAGE is kept as this lane's rollback (OVERLAY_FLASH=1 ./install.sh)."
    fi
    # The 1m limits the generator writes are static, and their worst case
    # (compaction at about 680,000 plus 200,000 of output) sits ABOVE the
    # 863,398-token floor this pool has been measured at: on an unlucky boot a
    # long session meets the proxy's refusal mid-conversation, which is the
    # field case that produced oc-fit-limits.py in the first place. That was a
    # documented manual step while 1m was opt-in. It cannot stay one now that
    # 1m is what a plain install serves.
    if [ "$CONTEXT_MODE" = "1m" ] && [ "$OPENCODE" -eq 1 ]; then
      echo "fitting the opencode limits to the KV pool this boot actually got:"
      # --restart-agent: opencode-web was restarted above, before this fit rewrote its
      # config, and it reads that config only at startup.
      python3 "$REPO_DIR/oc-fit-limits.py" --engine "http://127.0.0.1:$PORT" --restart-agent \
        || echo "  NOTE: could not fit them; run python3 oc-fit-limits.py yourself, or the cockpit's button"
    fi

    # ── 10/10 ───────────────────────────────────────────────────────────────
    # The cockpit is installed here, not by a second command the operator has to
    # find: a one-liner that leaves an engine and no way to drive it is half an
    # install. It runs AFTER the engine answered a real generation, so the page
    # it opens on is a working box rather than a loading screen, and a cockpit
    # that fails to install never fails the engine that is already serving.
    COCKPIT_URL=""
    if [ "$COCKPIT" -eq 1 ]; then
      step "10/10 Spark Cockpit (the web UI this box is meant to be driven from)"
      DASH_ENV=()
      if [ ! -f /etc/systemd/system/qwen38-dashboard.service ]; then
        # First install only. A re-run passes nothing, so install-dashboard.sh
        # converges on the bind and port already installed: an upgrade must
        # never flip a reachable cockpit back to loopback.
        # Loopback alone is useless on a headless box and 0.0.0.0 puts the login
        # on every interface, so the default is the tailnet address when the box
        # has one: private by construction, reachable from a laptop or a phone.
        TS_IP4="$(tailscale ip -4 2>/dev/null | head -1 || true)"
        if [ -n "$TS_IP4" ]; then
          DASH_ENV=(DASH_BIND="$TS_IP4")
          echo "first cockpit install: binding the tailnet address $TS_IP4 (the API key is the only gate)"
        else
          echo "first cockpit install: no tailnet address on this box, binding 127.0.0.1."
          echo "  to reach it from another machine: DASH_BIND=0.0.0.0 ./dashboard/install-dashboard.sh"
        fi
      fi
      if env ${DASH_ENV[@]+"${DASH_ENV[@]}"} "$REPO_DIR/dashboard/install-dashboard.sh"; then
        # The Agent tab needs opencode on PATH. Not having it costs one tab, and
        # one missing tab is not a reason to fail an install that is otherwise up.
        if [ "$OPENCODE" -eq 1 ] && command -v opencode >/dev/null 2>&1; then
          "$REPO_DIR/dashboard/install-agent.sh" \
            || echo "NOTE: the Agent tab did not install; the rest of the cockpit is up (retry: ./dashboard/install-agent.sh)"
        elif [ "$OPENCODE" -eq 1 ]; then
          echo "NOTE: opencode is not on your PATH (step 7 did not install it: see its NOTE, or OPENCODE_PIN=0),"
          echo "      so the Agent tab is not installed. Re-run ./install.sh, or install it and run ./dashboard/install-agent.sh"
        fi
        # The URL to print is the installed unit's own bind and port, read back
        # rather than assumed: install-agent.sh re-renders that unit, and a
        # converged re-run may be serving on an address this run never chose.
        CK_UNIT=/etc/systemd/system/qwen38-dashboard.service
        CK_PORT="$({ grep -m1 -E '^Environment=COCKPIT_PORT=' "$CK_UNIT" || true; } | cut -d= -f3-)"
        CK_BIND="$({ grep -m1 -E '^Environment=COCKPIT_BIND=' "$CK_UNIT" || true; } | cut -d= -f3-)"
        CK_HOST="${CK_BIND:-127.0.0.1}"
        case "$CK_HOST" in
          0.0.0.0|::|"[::]")
            # A wildcard bind is not an address you can type: name one.
            CK_HOST="$(tailscale ip -4 2>/dev/null | head -1 || true)"
            [ -n "$CK_HOST" ] || CK_HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"
            [ -n "$CK_HOST" ] || CK_HOST="127.0.0.1" ;;
        esac
        COCKPIT_URL="http://$CK_HOST:${CK_PORT:-30090}"
      else
        echo "NOTE: the cockpit did not install. The engine above is up and serving."
        echo "      retry with ./dashboard/install-dashboard.sh (logs: journalctl -u qwen38-dashboard -n 30)"
      fi
    elif systemctl is-active --quiet qwen38-dashboard.service 2>/dev/null; then
      # --no-cockpit with one already running: it imports this repo's python at
      # start, so an update that leaves it running serves the new html against
      # the old python in memory (2026-09-08: the switch selector offered a
      # target the action layer refused). Restart it, do not reinstall it.
      echo "restarting the cockpit so it picks up this repo's code (it imports it at start)"
      sudo systemctl restart qwen38-dashboard.service \
        || echo "  NOTE: could not restart it; do it by hand or its controls and its checks disagree"
    fi

    # ── The image lane, last, because it is the only step that stops the engine
    # that is already serving above, and the only one that costs 38 GB. It is
    # opt-in, and remembered: a box that has it keeps it across upgrades.
    IMAGE_ON=0
    [ -f /etc/systemd/system/qwen38-image.service ] && IMAGE_ON=1
    [ "$WITH_IMAGE" -eq 1 ] && IMAGE_ON=1
    [ "$NO_IMAGE" -eq 1 ] && IMAGE_ON=0
    if [ "$IMAGE_ON" -eq 1 ]; then
      step "Qwen-Image 2.1 lane (text-to-image, editing, native RGBA)"
      # The smoke test stops the engine this run just verified, loads 31 GB and puts it
      # back: minutes of unserved traffic. Worth it once, on the install that asked for
      # the lane; not on every routine upgrade of a box that happens to have it.
      IMAGE_ARGS=()
      [ "$WITH_IMAGE" -eq 0 ] && IMAGE_ARGS=(--no-smoke)
      if "$REPO_DIR/install-image.sh" ${IMAGE_ARGS[@]+"${IMAGE_ARGS[@]}"}; then
        IMAGE_READY=1
      else
        echo "NOTE: the image lane did not install. Everything above is up and serving."
        echo "      retry on its own with ./install-image.sh (it resumes what it already did)"
      fi
    fi

    printf '\n\033[1;32m✅ Installed, verified, and enabled at boot.\033[0m\n'
    if [ -n "$COCKPIT_URL" ]; then
      printf '\n\033[1;36m  ▶ OPEN THE COCKPIT:  %s\033[0m\n' "$COCKPIT_URL"
      echo "    log in with the API key in $CONFIG_DIR/api-key"
      echo
      echo "    Start, stop and switch the served model, watch every live request,"
      echo "    read the engine logs, run the benchmarks and drive an agent, from"
      echo "    that page. Nothing else needs installing or starting by hand."
      echo
      echo "  Raw endpoints, for clients that ask for them:"
    else
      echo
    fi
    # The proxy for every client: it passes every route of the engine through, with its
    # guards. These lines named the engine's own port, which listens on loopback since
    # v1.17 (unreachable from another machine) and has none of the guards (found in
    # review, 2026-09-24).
    echo "  Clients    : http://<host>:$PROXY_PORT (keepalive proxy, for opencode and every other client)"
    echo "  OpenAI     : http://<host>:$PROXY_PORT/v1/chat/completions"
    echo "  Anthropic  : http://<host>:$PROXY_PORT/v1/messages   (Bearer auth only)"
    echo "  Engine     : $ENGINE_BIND:$PORT, behind the proxy (ENGINE_BIND=0.0.0.0 opens it)"
    echo "  API key    : $CONFIG_DIR/api-key"
    if [ "$OPENCODE" -eq 1 ]; then
      OC_NOW="$( { opencode --version 2>/dev/null || true; } | tail -1 | tr -d 'v[:space:]')"
      # What starts it from the user's own shell: this script's PATH is not theirs, the
      # launcher is skipped when another oc exists, and ~/.local/bin may be new.
      OC_START="oc"
      if ! grep -qs 'dgx-spark-qwen38' "$HOME/.local/bin/oc"; then
        OC_START="${OC_OUT_CAP:+OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX=$OC_OUT_CAP }opencode --yolo"
      elif [ "$(command -v oc || true)" != "$HOME/.local/bin/oc" ]; then
        OC_START="$HOME/.local/bin/oc"
      fi
      OC_READS="${OC_USER_CFG:-$HOME/.config/opencode/opencode.json}"
      if [ -z "$OC_NOW" ]; then
        echo "  opencode   : NOT installed (see step 7). Its config is ready at $OC_READS;"
        echo "               re-run ./install.sh to install it, or get it from https://opencode.ai"
      elif [ "$OC_NOW" = "$OPENCODE_VERSION" ]; then
        echo "  opencode   : $OC_NOW, the version this repo tests; start it with: $OC_START   (config: $OC_READS)"
      else
        echo "  opencode   : $OC_NOW (this repo tests $OPENCODE_VERSION); start it with: $OC_START   (config: $OC_READS)"
      fi
    else
      echo "  opencode   : integration off (--no-opencode); ./install.sh --with-opencode turns it on"
    fi
    [ "$COCKPIT" -eq 0 ] && echo "  cockpit    : off (--no-cockpit); ./install.sh --with-cockpit turns it on"
    if [ "${IMAGE_READY:-0}" -eq 1 ]; then
      echo "  Images     : a third lane; switch to Qwen-Image 2.1 in the cockpit (or ./switch-model.sh image)"
    elif [ "${IMAGE_ON:-0}" -eq 0 ]; then
      echo "  Images     : not installed; ./install.sh --with-image adds the Qwen-Image 2.1 lane (38 GB)"
    fi
    [ "$LANE" = "27b" ] && echo "  Benchmark  : ./bench.sh"
    exit 0
  fi
  ST="$(systemctl is-active "$UNIT_NAME" || true)"
  [ "$ST" = "failed" ] && { journalctl -u "$UNIT_NAME" --no-pager | tail -25; die "Service failed during startup, logs above. Common cause: another process eating GPU/unified memory (this config needs the machine to itself)."; }
  NOW_RESTARTS="$(engine_restarts)"
  if [ -n "$NOW_RESTARTS" ] && [ -n "$ENGINE_RESTARTS0" ] && [ "$NOW_RESTARTS" -gt "$ENGINE_RESTARTS0" ]; then
    journalctl -u "$UNIT_NAME" --no-pager | tail -25
    die "The engine died during startup and systemd is relaunching it (Restart=always, $((NOW_RESTARTS - ENGINE_RESTARTS0)) relaunch(es) so far): logs above. Stop the loop with: sudo systemctl stop $UNIT_NAME"
  fi
  [ $((i % 15)) -eq 0 ] && echo "  still loading... ($((i*8))s; $LOAD_WHY)"
  sleep 8
done
die "Server did not come up within 20 min. Watch: journalctl -u $UNIT_NAME -f"
