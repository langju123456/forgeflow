#!/usr/bin/env bash
# vLLM serving figures on a Kaggle GPU, in one cell: TTFT, TPOT, throughput, p50 / p95 at concurrency 1 to 16
# and GPU memory, for the answer model of the 年报问答 desk (Qwen3-8B) or the nearest one that fits the cards.
#
# Notebook settings: Accelerator = GPU T4 x2 (a P100 will not do: vLLM needs compute capability 7.0 or higher
# and a P100 is 6.0), Internet on. One cell, then Save Version -> Save & Run All:
#   !git clone -q https://github.com/langju123456/forgeflow.git /kaggle/working/forgeflow
#   !bash /kaggle/working/forgeflow/scripts/kaggle_vllm_bench.sh
# (add `-b <branch>` to the clone for as long as this script is not on the default branch)
#
# What it does: installs vLLM, checks the GPUs, then runs scripts/vllm_bench.py with the candidates in
# VLLM_BENCH_MODELS in order; the first that fits and starts is measured, the rest are recorded as skipped
# or failed:
#   Qwen/Qwen3-8B       fp16, about 16 GB of weights: spread over both T4s; skipped when there is only one
#   Qwen/Qwen3-8B-AWQ   the 4-bit build of the same model: fits on one T4
#   Qwen/Qwen3-4B       one size down, fp16: fits on one T4
# Settings are environment variables, set in front of `bash` in the cell:
#   VLLM_BENCH_MODELS   space-separated candidates, in order of preference
#   VLLM_BENCH_ARGS     extra arguments for scripts/vllm_bench.py, e.g. "--output-len 256 --max-model-len 8192"
#   VLLM_VERSION        vLLM version to install (default below)
#   VLLM_BENCH_DRY_RUN  1 = no install and no GPU: print what would run, to check this script itself
# Progress is in /kaggle/working/STATUS.md. Results (report.json, summary.md, server.log, one bench file per
# concurrency level) are in /kaggle/working/vllm-bench and packed as /kaggle/working/vllm_bench_results.zip;
# to keep a run, copy its folder into preparation/s5/vllm-serving/runs/ of the repository.
# Time: the 16 GB download, server start and five levels are expected to take roughly half an hour of GPU
# time with the defaults; that is an estimate, nothing here has been measured yet.
set -uo pipefail

WORK="${KAGGLE_WORK:-/kaggle/working}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
STATUS="$WORK/STATUS.md"
ZIP="$WORK/vllm_bench_results.zip"
OUT="$WORK/vllm-bench"
MODELS="${VLLM_BENCH_MODELS:-Qwen/Qwen3-8B Qwen/Qwen3-8B-AWQ Qwen/Qwen3-4B}"
EXTRA="${VLLM_BENCH_ARGS:-}"
# 0.31.0 is a version known to run on a Kaggle T4 (it brings torch 2.13.0+cu130).
VLLM_VERSION="${VLLM_VERSION:-0.31.0}"
DRY_RUN="${VLLM_BENCH_DRY_RUN:-0}"
mkdir -p "$WORK" "$OUT"
cd "$REPO" || exit 1

note() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$STATUS"; }
package() { (cd "$WORK" && rm -f "$ZIP" && zip -qr "$ZIP" "$(basename "$OUT")" "$(basename "$STATUS")") && note "packed: $ZIP"; }
stop() { note "stopped: $*"; package; exit 1; }

echo "# vLLM serving benchmark" > "$STATUS"
note "start, code $(git rev-parse --short HEAD 2>/dev/null || echo '?'), candidates: $MODELS$([ "$DRY_RUN" = 1 ] && echo ' (dry run)')"

model_args=()
for model in $MODELS; do model_args+=(--model "$model"); done
# shellcheck disable=SC2206  # EXTRA is a list of arguments on purpose
extra_args=($EXTRA)
command=(python scripts/vllm_bench.py "${model_args[@]}" --out "$OUT" ${extra_args[@]+"${extra_args[@]}"})

if [ "$DRY_RUN" = 1 ]; then
  note "[dry run] pip install -q \"vllm[bench]==$VLLM_VERSION\"; pip uninstall -y -q torchaudio torchao"
  note "[dry run] ${command[*]}"
  package
  exit 0
fi

# ---- 1. environment ([bench] adds what `vllm bench` needs in versions that define that extra; pip ignores it otherwise)
pip install -q "vllm[bench]==$VLLM_VERSION" || stop "pip install vllm failed"
# The preinstalled torchaudio is built for another CUDA version and breaks `import transformers` after vLLM
# replaces torch; nothing here needs it, nor torchao.
pip uninstall -y -q torchaudio torchao >/dev/null 2>&1
python - <<'PY' 2>&1 | tee -a "$STATUS"; [ "${PIPESTATUS[0]}" = 0 ] || stop "environment check failed (see the line above)"
import torch, transformers, vllm
names = [torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())]
print("environment", {"vllm": vllm.__version__, "torch": torch.__version__, "transformers": transformers.__version__}, names)
assert torch.cuda.is_available(), "no GPU: set Accelerator to GPU T4 x2"
assert torch.cuda.get_device_capability(0) >= (7, 0), "vLLM needs compute capability 7.0 or higher, a P100 is 6.0: set Accelerator to GPU T4 x2"
PY

# ---- 2. serve and measure (the script stops the server itself, also when it fails)
note "running: ${command[*]}"
"${command[@]}" 2>&1 | tee -a "$WORK/vllm_bench_output.log"
code="${PIPESTATUS[0]}"
summary="$(find "$OUT" -name summary.md -newer "$STATUS" 2>/dev/null | sort | tail -1)"
[ -n "$summary" ] && cat "$summary" >> "$STATUS"
[ "$code" = 0 ] || stop "no candidate could be measured (exit $code); server.log of each attempt is in $OUT"
note "done: $summary"
package
