#!/usr/bin/env bash
# Can this GPU serve the flash lane's N-gram table from a file on NVMe? (issue #54)
#
# The flash lane serves its 47.7 GiB FP8 N-gram (PLE) table from a sparse file on NVMe
# (--ple-offload-backend file, sglang#37068), and the GPU reads its rows through the host page
# tables. A GB10 does that in its ATS addressing mode. Outside it SGLang refuses the file backend
# when it loads the weights ("needs a device whose pageable host memory accesses go through the
# host page tables"): on the box of issue #54, whose mode reads None, that came after the image and
# the checkpoint had been fetched, and systemd started the lane again every 15 s. nvidia-smi names
# the mode: ATS, HMM or None. The reference box runs in ATS; the box of issue #54 boots with the
# same iommu.passthrough=0 and the same nvidia_uvm settings, so those alone do not decide it.
# The answer holds for the whole lane: its FP4-Local config (b12x vLLM) reads the table from the
# checkpoint's safetensors, not from such a file, and whether it needs ATS is not measured, so
# its launcher asks too and the line below claims the file for the SGLang config only (found in
# review, 2026-10-20).
#
# Prints one line. Exits 1 when nvidia-smi names HMM or None (the line says why), 0 otherwise: ATS,
# and anything else (no nvidia-smi, a driver without the field, [N/A], no answer in time), where
# the engine decides as it did before. QWEN38_PREFLIGHT_SMI and QWEN38_PREFLIGHT_SMI_TIMEOUT_S are
# engine-preflight.sh's (the tests name another nvidia-smi).
#
# usage: flash-ats-check.sh
set -u
SMI="${QWEN38_PREFLIGHT_SMI:-nvidia-smi}"
SMI_TIMEOUT_S="${QWEN38_PREFLIGHT_SMI_TIMEOUT_S:-10}"
mode=""
if out="$(timeout -k 2 "$SMI_TIMEOUT_S" "$SMI" --query-gpu=addressing_mode --format=csv,noheader 2>/dev/null)"; then
  mode="$(printf '%s\n' "$out" | head -1 | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
fi
case "$mode" in
  ATS) echo "addressing mode ATS" ;;
  HMM|None)
    echo "the GPU's addressing mode is $mode (nvidia-smi --query-gpu=addressing_mode) and the flash lane needs ATS: its SGLang config reads its 47.7 GiB N-gram table from a file on NVMe through the host page tables, and the guard holds for the whole lane, FP4-Local too, whose need for ATS is not measured (docs/flash-lane.md)"
    exit 1 ;;
  *) echo "addressing mode not reported by nvidia-smi${mode:+ ($mode)}, the engine decides" ;;
esac
