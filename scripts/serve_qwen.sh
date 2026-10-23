#!/usr/bin/env bash
# Start a vLLM OpenAI-compatible server for Option B (open-weight backend).
#
#   bash scripts/serve_qwen.sh vision [GPU] [PORT]   # Qwen3-VL for Director/Composer/critics/verifier
#   bash scripts/serve_qwen.sh coder  [GPU] [PORT]   # Qwen3-Coder for the coding agent
#
# Models come from config.sh (LLM_OPENAI_MODEL / LLM_CODER_MODEL). Override memory use with
# GPU_MEM_UTIL (fraction of the card, default 0.85) when the GPU is shared.
# Needs vLLM in the current env (pip install vllm) — or set VLLM_BIN=/path/to/vllm.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
. "$ROOT/config.sh"
ROLE=${1:?usage: serve_qwen.sh vision|coder [GPU] [PORT]}
GPU=${2:-${GPUS%%,*}}
VLLM=${VLLM_BIN:-vllm}
UTIL=${GPU_MEM_UTIL:-0.85}
case "$ROLE" in
  vision)
    MODEL=$LLM_OPENAI_MODEL; PORT=${3:-8000}
    ARGS=(--max-model-len "${VLLM_MAX_LEN:-32768}" --limit-mm-per-prompt '{"image":6}') ;;
  coder)
    MODEL=$LLM_CODER_MODEL; PORT=${3:-8001}
    ARGS=(--max-model-len "${VLLM_MAX_LEN:-65536}" --enable-auto-tool-choice --tool-call-parser qwen3_coder) ;;
  *) echo "role must be vision or coder"; exit 1 ;;
esac
echo "[serve_qwen] $ROLE: $MODEL on GPU $GPU, port $PORT (gpu_memory_utilization=$UTIL)"
export CUDA_VISIBLE_DEVICES="$GPU"
exec "$VLLM" serve "$MODEL" --port "$PORT" --served-model-name "$MODEL" \
     --gpu-memory-utilization "$UTIL" --trust-remote-code "${ARGS[@]}" "${@:4}"
