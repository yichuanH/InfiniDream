#!/usr/bin/env bash
# Build one baseline Infinigen scene that the pipeline can edit.
#
#   bash scripts/build_scene.sh <biome> <seed> [--no-fine] [--angle auto|ground|raised|eye|aerial|closeup]
#
# Produces scenes/<biome>/seed<seed>/
#   coarse/scene.blend     terrain + placeholders + the native camera   (required by the pipeline)
#   fine/scene.blend       populated + fine terrain, native camera      (optional: Director preview)
#   preview/               5 candidate camera angles + contact_sheet.png (only with --angle)
#   camera_choice.json     chosen camera pose                            (only with --angle)
#
# Biomes: forest desert coast mountain plain canyon cliff river arctic snowy_mountain cave
#         coral_reef under_water kelp_forest
# Underwater biomes automatically get third_party/infinigen_configs/underwater_fix.gin.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
. "$ROOT/config.sh"
BIOME=${1:?usage: build_scene.sh <biome> <seed> [--no-fine] [--angle NAME]}
SEED=${2:?usage: build_scene.sh <biome> <seed> [--no-fine] [--angle NAME]}
shift 2
WITH_FINE=1; ANGLE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --no-fine) WITH_FINE=0 ;;
    --angle) ANGLE=$2; shift ;;
    *) echo "unknown option $1"; exit 1 ;;
  esac; shift
done
PY=$BI_PYTHON; INF=$INFINIGEN_ROOT
D="$ROOT/scenes/$BIOME/seed$SEED"
mkdir -p "$D"
export CUDA_VISIBLE_DEVICES="${GPUS%%,*}"
export PYTHONPATH="$ROOT/core:$INF:${PYTHONPATH:-}"

# Same gin parameters the reference scene bank was built with (terrain variety pool
# capped at 1 instance for speed; 40 m populate radius). Changing them changes the scene.
INST="infinigen.terrain.elements.landtiles.load_assets.on_the_fly_instances=1 infinigen.terrain.elements.caves.load_assets.on_the_fly_instances=1"
COMMON_P="compose_nature.inview_distance=40 $INST"
GINS="$BIOME.gin"
case "$BIOME" in
  coral_reef|under_water|kelp_forest)
    GINS="$GINS $ROOT/third_party/infinigen_configs/underwater_fix.gin" ;;
esac

log(){ echo "[$(date +%H:%M:%S)] [$BIOME/seed$SEED] $*"; }
cd "$INF"

if [ ! -s "$D/coarse/scene.blend" ]; then
  log "coarse ..."
  $PY -m infinigen_examples.generate_nature --seed "$SEED" --task coarse \
    --output_folder "$D/coarse" -g $GINS -p $COMMON_P > "$D/coarse.log" 2>&1 \
    || { tail -30 "$D/coarse.log"; log "coarse FAILED"; exit 1; }
  log "coarse OK"
else
  log "coarse exists, skipping"
fi

if [ "$WITH_FINE" = 1 ] && [ ! -s "$D/fine/scene.blend" ]; then
  log "populate + fine_terrain (native camera) ..."
  $PY -m infinigen_examples.generate_nature --seed "$SEED" --task populate fine_terrain \
    --input_folder "$D/coarse" --output_folder "$D/fine" -g $GINS -p $COMMON_P \
    > "$D/populate.log" 2>&1 \
    || { tail -30 "$D/populate.log"; log "populate/fine FAILED"; exit 1; }
  log "fine OK"
fi

if [ -n "$ANGLE" ]; then
  cd "$ROOT"
  if [ "$ANGLE" = auto ]; then
    # Render 5 candidate poses from coarse (plus the populated view if fine exists)
    # and let Claude pick one; writes preview/camera_choice.json.
    log "pick camera (agent) ..."
    VIS=""; [ -s "$D/fine/scene.blend" ] && VIS="--visual-blend $D/fine/scene.blend"
    $PY tools/photographer/pick_camera.py "$D/coarse/scene.blend" "$D/preview" $VIS > "$D/preview.log" 2>&1 \
      || { tail -20 "$D/preview.log"; log "pick_camera FAILED"; exit 1; }
    cp "$D/preview/camera_choice.json" "$D/camera_choice.json"
  else
    $PY tools/scene_bank/make_camera_choice.py "$ANGLE" "$D/camera_choice.json" \
      --scene "$BIOME/seed$SEED" --blend "$D/coarse/scene.blend"
  fi
  log "camera_choice.json -> $(python3 -c "import json;print(json.load(open('$D/camera_choice.json'))['angle'])")"
fi
log "DONE  scene dir: $D"
