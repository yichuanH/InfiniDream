#!/usr/bin/env bash
# Generate ONE procedural asset outside the scene pipeline (for testing a backend / a prompt).
#
#   bash scripts/gen_asset.sh <name> <InfinigenFactory|auto> "<description>" [--target ref.png] [--rounds N]
#
# Example:
#   bash scripts/gen_asset.sh clownfish FishFactory "An orange clownfish with three white bands" --target refs/clownfish.png
#
# Output: output/assets_standalone/<name>/{final.png, plan.md, r1..rN/, code/overlay/, score.json}
# Uses the LLM backend selected in config.sh (LLM_BACKEND=claude|openai) and the GPU in GPUS.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
. "$ROOT/config.sh"
NAME=${1:?usage: gen_asset.sh <name> <factory|auto> "<description>" [--target img] [--rounds N]}
FACTORY=${2:?}; PROMPT=${3:?}; shift 3
TARGET=""; ROUNDS=${ROUNDS:-3}; EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --target) TARGET=$2; shift ;;
    --rounds) ROUNDS=$2; shift ;;
    *) EXTRA+=("$1") ;;
  esac; shift
done
OUT=${ASSET_OUT:-$ROOT/output/assets_standalone}/$NAME
mkdir -p "$OUT"
[ -s "$ROOT/.env" ] || [ "$LLM_BACKEND" != claude ] || { echo "⛔ .env missing (CLAUDE_API_KEY)"; exit 1; }
export CUDA_VISIBLE_DEVICES="${GPUS%%,*}"
export PYTHONPATH="$ROOT/core:$INFINIGEN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
cd "$ROOT"
ARGS=(--prompt "$PROMPT" --name "$NAME" --factory "$FACTORY" --rounds "$ROUNDS" --out-dir "$OUT" --no-turntable --no-seed-check)
[ -n "$TARGET" ] && ARGS+=(--target "$(readlink -f "$TARGET")")
echo "[gen_asset] backend=$LLM_BACKEND gpu=$CUDA_VISIBLE_DEVICES out=$OUT"
"$BI_PYTHON" asset_generator/extraworld/3_procedural_generator.py "${ARGS[@]}" "${EXTRA[@]}" 2>&1 | tee "$OUT/run.log"
rc=${PIPESTATUS[0]}
[ "$rc" -eq 0 ] && echo "✅ $NAME → $OUT/final.png" || echo "⛔ $NAME failed rc=$rc (see $OUT/run.log)"
exit "$rc"
