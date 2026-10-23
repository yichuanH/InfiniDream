#!/usr/bin/env bash
# Run the full text → edited-scene → video pipeline for one prompt.
#
#   bash scripts/run_case.sh <case_name> <biome> <seed> "<prompt text>"
#   bash scripts/run_case.sh <case_name> <biome> <seed> @examples/cases/sakura_spoonbills.txt
#
# Requires scenes/<biome>/seed<seed>/coarse/scene.blend (see scripts/build_scene.sh).
# Outputs land in output/runs/<case_name>/ ; the final video is render/final.mp4 there.
#
# Useful env overrides (see config.sh for the rest):
#   GPUS=0,1            which GPUs to use
#   RENDER_VIDEO=0      stop after the validated still image (much faster)
#   STOP_AFTER_DIRECTOR=1   only plan the edits (no asset generation / rendering)
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
. "$ROOT/config.sh"
CASE=${1:?usage: run_case.sh <case_name> <biome> <seed> <prompt|@file>}
BIOME=${2:?}; SEED=${3:?}; PROMPT=${4:?}
case "$PROMPT" in @*) PROMPT=$(cat "${PROMPT#@}") ;; esac
[ -s "$ROOT/.env" ] || { echo "⛔ $ROOT/.env missing — copy .env.example and fill in CLAUDE_API_KEY"; exit 1; }
SCENE_DIR="$ROOT/scenes/$BIOME/seed$SEED"
[ -s "$SCENE_DIR/coarse/scene.blend" ] || { echo "⛔ no scene at $SCENE_DIR — run: bash scripts/build_scene.sh $BIOME $SEED"; exit 1; }

RUN="$ROOT/output/runs/$CASE"
mkdir -p "$RUN/input"
printf '%s\n' "$PROMPT" > "$RUN/input/prompt.txt"
export SCENE_CHOICE="$BIOME" SCENE_SEED="$SEED"
export E2E_INPUT_DIR="$RUN/input" E2E_RUN_DIR="$RUN/run"
cd "$ROOT"
bash pipeline/run_e2e.sh "$CASE" "${FROM_STEP:-1}" 2>&1 | tee "$RUN/e2e.log"
rc=${PIPESTATUS[0]}
# Collect deliverables: output/final/<case>/{output/{before,after}.{png,mp4,gif}, obj/<asset>/, info.txt}
ST=FAIL; [ "$rc" -eq 0 ] && ST=PASS
BV=(); [ "$ST" = PASS ] && [ "${BEFORE_VIDEO:-1}" = 1 ] && BV=(--before-video --gpu "${GPUS%%,*}")
CUDA_VISIBLE_DEVICES=${GPUS%%,*} "$BI_PYTHON" pipeline/finalize_case.py --run-dir "$RUN/run" --case "$CASE" \
  --scene "$BIOME" --seed "$SEED" --status "$ST" --prompt-file "$RUN/input/prompt.txt" \
  --final-root "${FINAL_ROOT:-$ROOT/output/final}" "${BV[@]}" 2>&1 | tail -3
if [ "$rc" -eq 0 ]; then
  echo; echo "✅ $CASE finished  → ${FINAL_ROOT:-$ROOT/output/final}/$CASE/output/"
  [ -s "$RUN/run/render/final.mp4" ] && echo "   video: $RUN/run/render/final.mp4"
  [ -s "$RUN/run/render/view.png" ] && echo "   still: $RUN/run/render/view.png"
else
  echo; echo "⛔ $CASE failed (rc=$rc) — see $RUN/e2e.log"
fi
exit "$rc"
