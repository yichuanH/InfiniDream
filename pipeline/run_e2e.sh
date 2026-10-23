#!/bin/bash
# ★★一條龍：prompt → 場景 → 計畫 → 生資產 → 改 coarse → populate → genuine fine → validate → render★★
#   (2026-08-06 首次寫。在此之前每一段都能跑，但★沒有任何腳本把它們接起來★ ——
#    manifest 的 code 欄一直是各測試腳本手填的，階段 B 從沒被自動叫過。)
#
# 用法:  bash pipeline/run_e2e.sh <case名> [起始步驟]
#        起始步驟 = 1..12，用來續跑(例如資產已經生好就從 5 開始)
#        E2E_PROMPT="..." bash ...                            # 沒有 input/<case>/prompt.txt 時建立 prompt
#        E2E_BASELINE_PICKER=0 bash ...                       # 回到舊 pick_scene + seed0
#        CAMERA_CHOICE=/path/to/camera_choice.json bash ...  # 明確指定固定相機
#        SCENE_DIR=/path/to/<scene>/seed0 bash ...            # 明確指定 coarse/fine 組
#        SCENE_CHOICE=forest SCENE_SEED=0 bash ...            # 指定 biome+seed，跳過挑場景
#        STOP_AFTER_PREPARE=1 bash ...                        # 只驗證 baseline pick + input symlink
#        STOP_AFTER_PLACEMENT_REVIEW=1 bash ...               # 看 Step-8 白盒並讓 agent 改 JSON
#        STOP_AFTER_DIRECTOR=1 bash ...                       # 只跑到 step 4，交出 edit_plan.json 供人工 check
#        PLACEMENT_ONLY_E2E=1 bash ...                         # Director→Composer→Step 8；不生資產/populate
#        E2E_INPUT_DIR=/path/to/input bash ...                 # isolated benchmark input (prompt/blend/render)
#        COMPOSER_IMAGE_PLACEMENT=1 bash ...                   # Composer sees original render, code raycasts/snaps
#        STOP_AFTER_POPULATE=1 bash ...                       # 驗證到 step 9 即停
#        ASSET_TURNTABLE=0 ASSET_SEED_CHECK=0 bash ...        # 省略非必要資產 QA render
#        RENDER_VIDEO=0 bash ...                              # 只出 validated still 的 smoke run
#
# 步驟:
#   1 pick_scene    prompt → 挑 top-2 scene groups → 看 10 張 seed preview 選 baseline
#   2 prepare       把 picked baseline blend / render.png 連進 input/<case>/
#   3 camera        有指定就固定使用；否則保留 Infinigen 官方 active camera
#   4 director      LLM① → edit_plan.json(anchoring / n_masters / procedural-only route)
#   5 genlist+gen   ★階段 B★ edit_plan → generation_list → ProceduralGenerator(GPU 並行)
#   6 overlays      生成產物 → ★分流成 per-asset overlay roots★ + routing table
#   7 composer      LLM② → hero_edits/extra_edits + appearance.json
#   8 applyplan     ★階段 A★ 改 coarse blend、render placeholder preview、產 review contract
#   9 placer        ★階段 C★ custom/full populate + 貼地 → populated/scene.blend
#  10 fine_terrain  ★真正的 fine★ populated → fine/scene.blend（室內 prompt 會在 step 9 後自動停）
#  11 contract      驗地形/相機/物件/粒子/placer；建立 render gate
#  12 render        ★只准渲染通過 contract 的 exact fine blend★
set -u
cd "$(dirname "$0")/.."
ROOT=$(pwd)
# shared modules (core/) must be importable by every python we launch, including ones that set their own PYTHONPATH below
export PYTHONPATH="$ROOT/core${PYTHONPATH:+:$PYTHONPATH}"
CASE=${1:?用法: run_e2e.sh <case名> [起始步驟]}
FROM=${2:-1}
# ★Release★: interpreter / Infinigen checkout / tool paths come from config.sh or the
#   environment (see README). Defaults are repo-relative so a fresh clone just works.
[ -f "$ROOT/config.sh" ] && . "$ROOT/config.sh"
PY=${BI_PYTHON:-$(command -v python)}
INF=${INFINIGEN_ROOT:-$ROOT/third_party/infinigen}
[ -x "$PY" ] || { echo "⛔ python not found: $PY (activate the conda env or set BI_PYTHON)"; exit 1; }
[ -d "$INF/infinigen" ] || { echo "⛔ Infinigen checkout not found: $INF (set INFINIGEN_ROOT, see README)"; exit 1; }
export BI_PYTHON="$PY" INFINIGEN_ROOT="$INF"
export INFINIGEN_TEMPLATES=${INFINIGEN_TEMPLATES:-$ROOT/third_party/Infinigen_templates}
export BLENDER_PYTHON=${BLENDER_PYTHON:-$PY}
export CLAUDE_BIN=${CLAUDE_BIN:-$(command -v claude || echo claude)}
EW=$ROOT/asset_generator/extraworld
# Tests may opt into an explicit self-contained run directory.  The default is
# unchanged so existing commands and organized-output links keep working.
W=${E2E_RUN_DIR:-$ROOT/output/_test/e2e_$CASE}
INPUT_DIR=${E2E_INPUT_DIR:-$ROOT/input/$CASE}
mkdir -p "$W"
# ★★把 $W 解析成真實路徑★★(2026-08-09)
#   output/_test/e2e_<case> 現在是 symlink 指到 08_e2e_runs/<case>(整理 output 時加的)。
#   placer ★多處用字串比對路徑★(overlay 有沒有在 sys.path、import 到的是不是 overlay)，
#   symlink 路徑與真實路徑不相等 ⇒ 誤判成「overlay 沒掛上」/「import 到官方版」。
#   ⚠️ 一處一處改比對是錯的做法 —— ★在入口就統一成真實路徑★，整類問題一次消失。
W=$(cd "$W" && pwd -P)
# ⚠️ ★2026-09-08 修正：原本寫死 2,3("這台機器固定用 2,3")★
#    但這台機器現在只有 GPU 0 和 1(nvidia-smi -L)。CUDA_VISIBLE_DEVICES=2
#    看不到任何裝置 ⇒ Cycles ★靜默退回 CPU★，同場景一張圖從 17 秒變 82 分鐘，
#    而且沒有任何錯誤訊息。預設改成 0,1；退回 CPU 現在會由
#    stage_exec/render_device.py 印出醒目警告，本腳本再轉貼到主 log。
GPUS=${GPUS:-0,1}
# SEED is resolved after SCENE_DIR.  Keeping an unconditional default of 0
# here made an explicit scenes/<biome>/seed1 input use seed 0 at Step 10, so
# fine_terrain rebuilt a different set of terrain surfaces and crashed while
# looking for displacement modifiers from the original coarse scene.
SEED=${SEED:-}
# final-scene contract 同時定義 fine meshing 覆蓋的動畫範圍。正式預設對齊既有
# Infinigen forest deliverable：72 格 / 24fps = 3 秒。只有所有 world_motion=none
# 的純靜態 smoke 可設 FRAME_END=1；含 Director motion 的 still-only smoke 至少要
# 滿足各 preset 的 distinct-frame contract（目前公開 vocabulary 最多需要 6 格）。
# fine_terrain 與 render 必須消費同一段 frame range。
FRAME_START=${FRAME_START:-1}
FRAME_END=${FRAME_END:-72}
[ "$FRAME_START" -ge 1 ] 2>/dev/null || { echo "⛔ FRAME_START 必須是正整數"; exit 1; }
[ "$FRAME_END" -ge "$FRAME_START" ] 2>/dev/null || { echo "⛔ FRAME_END 必須 >= FRAME_START"; exit 1; }
FRAME_MID=$(( (FRAME_START + FRAME_END) / 2 ))
CONTRACT_FRAMES=$FRAME_START,$FRAME_MID,$FRAME_END
# 正式 pipeline 一律預設 OcMesher。SphericalMesher 的限制不是只看 active camera
# 單格 FOV：Infinigen 會合併 camera rig 的所有 sub-camera 與整段運鏡，expanded FOV
# 可能在單格只有 35° 時仍超過 90° 而 assert。SphericalMesher 只保留給顯式速度實驗。
# 舊的 FINE_MESHER=auto 為相容既有命令，也保守解析成 OcMesher。
FINE_MESHER=${FINE_MESHER:-OcMesher}
# 這條 pipeline 的 coarse 場景統一由輕量設定建立，每類 terrain asset pool 只有
# 1 個 instance。fine_terrain 必須沿用同一個數量；若省略，Infinigen 會回到官方
# 預設 5，並在 fine 階段無謂補生 #1..#4 的 caves / landtiles / mountains。
# 這裡刻意固定為 1，不能讓 step 10 自行退回預設 5。
TERRAIN_ASSET_INSTANCES=1
# 正式一條龍預設包含完整 MP4。renderer 只消費 blend 內既有相機、object actions
# 與 particle cache；不會 free/rebuild cache，也不會另造運鏡或粒子效果。
RENDER_VIDEO=${RENDER_VIDEO:-1}
CONNECTIVITY_SMOKE=${CONNECTIVITY_SMOKE:-0}
# 擴充資產庫。★預設開啟★(2026-08-20 接線驗證完成後翻正)：Step 4 會查庫，把語意上
# 已經做過的資產標成 REUSE，Step 5 直接跳過生成、Step 6 從庫重建 overlay。
# BI_LIBRARY=0 可完全關掉，行為與接庫前完全相同(查庫失敗本身也一律降級成 FRESH)。
BI_LIBRARY=${BI_LIBRARY:-1}
export BI_LIBRARY
# ★軟關卡★(2026-10-20 使用者：盡量改成警告)：STRICT=1 才會在品質關卡停下 —— placer contract、
#   final-scene contract、machine review 不收斂、部分資產生成失敗、world-motion 契約。
#   預設 STRICT=0：印 ⚠️ 繼續跑、照樣出圖；不合格的事實仍留在 placer_report / final_contract.json，
#   且★不會建立 final_contract.ok★(評估時用 STRICT=1 才算正式品質資格)。
STRICT=${STRICT:-0}
BI_SOFT_GATES=$([ "$STRICT" = 1 ] && echo 0 || echo 1)
export STRICT BI_SOFT_GATES
soft_die () { if [ "$STRICT" = 1 ]; then die "$@"; else echo "  ⚠️ (soft gate) $*  —— STRICT=0，繼續"; return 0; fi; }
VIDEO_FPS=${VIDEO_FPS:-24}
VIDEO_SAMPLES=${VIDEO_SAMPLES:-48}
VIDEO_RES=${VIDEO_RES:-960x540}
# The validated still keeps the production defaults, while an explicit
# connectivity smoke can request a cheap image without changing any scene or
# contract gate.  This only controls Step 12 render cost/quality.
STILL_RENDER_WIDTH=${STILL_RENDER_WIDTH:-1600}
STILL_RENDER_HEIGHT=${STILL_RENDER_HEIGHT:-900}
STILL_RENDER_SAMPLES=${STILL_RENDER_SAMPLES:-96}
case "$STILL_RENDER_WIDTH:$STILL_RENDER_HEIGHT:$STILL_RENDER_SAMPLES" in
  *[!0-9:]*) echo "⛔ STILL_RENDER_WIDTH / HEIGHT / SAMPLES must be positive integers"; exit 1 ;;
esac
if [ "$STILL_RENDER_WIDTH" -lt 1 ] || [ "$STILL_RENDER_HEIGHT" -lt 1 ] || \
   [ "$STILL_RENDER_SAMPLES" -lt 1 ]; then
  echo "⛔ STILL_RENDER_WIDTH / HEIGHT / SAMPLES must be positive integers"
  exit 1
fi
# 資產 turntable QA：正式品質預設 full（36 level + 36 top-down）；smoke 是
# 4 + 4，共 8 格。Connectivity 可明確指定 level-only 5 張，避免渲染動畫。
# 這只影響獨立資產 QA，不改 final scene render。
ASSET_QA_MODE=${ASSET_QA_MODE:-full}
case "$ASSET_QA_MODE" in
  smoke|full) ;;
  *) echo "⛔ ASSET_QA_MODE 必須是 smoke / full（收到 $ASSET_QA_MODE）"; exit 1 ;;
esac
ASSET_TURNTABLE_VIEWS=${ASSET_TURNTABLE_VIEWS:-both}
case "$ASSET_TURNTABLE_VIEWS" in
  level|both) ;;
  *) echo "⛔ ASSET_TURNTABLE_VIEWS must be level or both"; exit 1 ;;
esac
ASSET_TURNTABLE_FRAMES_PER_VIEW=${ASSET_TURNTABLE_FRAMES_PER_VIEW:-}
if [ -z "$ASSET_TURNTABLE_FRAMES_PER_VIEW" ]; then
  if [ "$ASSET_QA_MODE" = smoke ]; then
    ASSET_TURNTABLE_FRAMES_PER_VIEW=4
  else
    ASSET_TURNTABLE_FRAMES_PER_VIEW=36
  fi
fi
case "$ASSET_TURNTABLE_FRAMES_PER_VIEW" in
  ''|*[!0-9]*) echo "⛔ ASSET_TURNTABLE_FRAMES_PER_VIEW must be a positive integer"; exit 1 ;;
esac
[ "$ASSET_TURNTABLE_FRAMES_PER_VIEW" -ge 1 ] || \
  { echo "⛔ ASSET_TURNTABLE_FRAMES_PER_VIEW must be >= 1"; exit 1; }
ASSET_TURNTABLE=${ASSET_TURNTABLE:-1}
ASSET_SEED_CHECK=${ASSET_SEED_CHECK:-1}
STOP_AFTER_POPULATE=${STOP_AFTER_POPULATE:-0}
STOP_AFTER_PREPARE=${STOP_AFTER_PREPARE:-0}
STOP_AFTER_PLACEMENT_REVIEW=${STOP_AFTER_PLACEMENT_REVIEW:-0}
STOP_AFTER_DIRECTOR=${STOP_AFTER_DIRECTOR:-0}
PLACEMENT_ONLY_E2E=${PLACEMENT_ONLY_E2E:-0}
E2E_BASELINE_PICKER=${E2E_BASELINE_PICKER:-1}
INITIAL_PLACEMENT_TOP_PREVIEW=${INITIAL_PLACEMENT_TOP_PREVIEW:-1}
MACHINE_COMPOSER_REVIEW_ROUNDS=${MACHINE_COMPOSER_REVIEW_ROUNDS:-5}
MACHINE_COMPOSER_VISION=${MACHINE_COMPOSER_VISION:-0}
# Map-first deterministic surface/volume placement is the formal default. Composer
# still authors semantic surface policy, but no extra placement Agent/API call
# chooses regions or cells.  Set 0 only for explicit legacy diagnostics.
MACHINE_COMPOSER_REGION_MAP=${MACHINE_COMPOSER_REGION_MAP:-1}
# Experimental indoor path: Composer looks at the original render and proposes
# semantic screen points.  Deterministic map/raycast code snaps them to feasible
# supports before Machine Review.  This keeps the Agent out of grid/candidate
# selection while retaining the same geometric contract.
COMPOSER_IMAGE_PLACEMENT=${COMPOSER_IMAGE_PLACEMENT:-0}
COMPOSER_IMAGE_SURFACE_CHOICE=${COMPOSER_IMAGE_SURFACE_CHOICE:-0}
PLACEMENT_INDOOR_SURFACE_FALLBACK=${PLACEMENT_INDOOR_SURFACE_FALLBACK:-1}
PLACEMENT_SEMANTIC_GRID_PREVIEW=${PLACEMENT_SEMANTIC_GRID_PREVIEW:-0}
PLACEMENT_REGION_MAP_COLUMNS=${PLACEMENT_REGION_MAP_COLUMNS:-48}
PLACEMENT_REGION_MAP_ROWS=${PLACEMENT_REGION_MAP_ROWS:-27}
case "$ASSET_TURNTABLE:$ASSET_SEED_CHECK:$STOP_AFTER_PREPARE:$STOP_AFTER_PLACEMENT_REVIEW:$STOP_AFTER_DIRECTOR:$STOP_AFTER_POPULATE:$RENDER_VIDEO:$PLACEMENT_ONLY_E2E:$E2E_BASELINE_PICKER:$INITIAL_PLACEMENT_TOP_PREVIEW:$CONNECTIVITY_SMOKE:$COMPOSER_IMAGE_PLACEMENT:$COMPOSER_IMAGE_SURFACE_CHOICE:$PLACEMENT_INDOOR_SURFACE_FALLBACK" in
  *[!01:]*) echo "⛔ ASSET_TURNTABLE / ASSET_SEED_CHECK / STOP_AFTER_PREPARE / STOP_AFTER_PLACEMENT_REVIEW / STOP_AFTER_DIRECTOR / STOP_AFTER_POPULATE / RENDER_VIDEO / PLACEMENT_ONLY_E2E / E2E_BASELINE_PICKER / CONNECTIVITY_SMOKE / COMPOSER_IMAGE_PLACEMENT / COMPOSER_IMAGE_SURFACE_CHOICE / PLACEMENT_INDOOR_SURFACE_FALLBACK 只能是 0 或 1"; exit 1 ;;
esac
if [ "$CONNECTIVITY_SMOKE" = 1 ] && [ "$RENDER_VIDEO" = 1 ]; then
  echo "⛔ CONNECTIVITY_SMOKE=1 only supports RENDER_VIDEO=0"
  exit 1
fi
case "$MACHINE_COMPOSER_REVIEW_ROUNDS" in
  ''|*[!0-9]*) echo "⛔ MACHINE_COMPOSER_REVIEW_ROUNDS must be an integer"; exit 1 ;;
esac
case "$MACHINE_COMPOSER_VISION" in
  0|1) ;;
  *) echo "⛔ MACHINE_COMPOSER_VISION must be 0 or 1"; exit 1 ;;
esac
case "$MACHINE_COMPOSER_REGION_MAP" in
  0|1) ;;
  *) echo "⛔ MACHINE_COMPOSER_REGION_MAP must be 0 or 1"; exit 1 ;;
esac
case "$PLACEMENT_SEMANTIC_GRID_PREVIEW" in
  0|1) ;;
  *) echo "⛔ PLACEMENT_SEMANTIC_GRID_PREVIEW must be 0 or 1"; exit 1 ;;
esac
case "$PLACEMENT_REGION_MAP_COLUMNS:$PLACEMENT_REGION_MAP_ROWS" in
  *[!0-9:]*) echo "⛔ placement region-map rows/columns must be integers"; exit 1 ;;
esac
if [ "$PLACEMENT_REGION_MAP_COLUMNS" -lt 2 ] || \
   [ "$PLACEMENT_REGION_MAP_ROWS" -lt 2 ]; then
  echo "⛔ placement region map must be at least 2x2"
  exit 1
fi
if [ "$PLACEMENT_ONLY_E2E" = 1 ]; then
  STOP_AFTER_PLACEMENT_REVIEW=1
  [ "$FROM" -le 8 ] || { echo "⛔ PLACEMENT_ONLY_E2E 只允許從 step 1..8 啟動"; exit 1; }
fi
# 相機不由 pipeline 自動選。優先順序：CAMERA_CHOICE 指定檔、
# input/<case>/camera_choice.json、scenes/<scene>/seed0/camera_choice.json，
# 最後才是 Infinigen 官方 active camera。選定後所有下游只讀 scene.camera。
E2E_LOG_DIR=${E2E_LOG_DIR:-$ROOT/scratchpad}
mkdir -p "$W" "$E2E_LOG_DIR"
L=$E2E_LOG_DIR/e2e_${CASE}
# overlay_merged 現在只是 per-asset overlay staging container，不是單一合併樹。
OV=$W/overlay_merged
TIMING_FILE=$W/stage_timings.tsv
RUN_STARTED_EPOCH=$(date +%s)
RUN_STARTED_ISO=$(date --iso-8601=seconds)
CURRENT_STEP=
CURRENT_NAME=
CURRENT_STARTED=
printf 'run_started\t%s\nstep\tname\tstarted\tfinished\tduration_seconds\tstatus\n' \
    "$RUN_STARTED_ISO" > "$TIMING_FILE"

record_current_stage () {
  local status=$1 now finished_iso duration
  [ -n "$CURRENT_STEP" ] || return 0
  now=$(date +%s)
  finished_iso=$(date --iso-8601=seconds)
  duration=$(( now - CURRENT_STARTED ))
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$CURRENT_STEP" "$CURRENT_NAME" "$CURRENT_STARTED_ISO" \
      "$finished_iso" "$duration" "$status" >> "$TIMING_FILE"
  CURRENT_STEP=
}

step () {
  record_current_stage complete
  CURRENT_STEP=$1
  CURRENT_NAME=$2
  CURRENT_STARTED=$(date +%s)
  CURRENT_STARTED_ISO=$(date --iso-8601=seconds)
  echo
  echo "════════ [$1] $2  $(date +%T) ════════"
}
die  () { echo "⛔ $*"; exit 1; }

organize_outputs () {
  "$PY" pipeline/organize_e2e_outputs.py \
      --run-root "$W" --input-dir "$INPUT_DIR" --log-prefix "$L" \
      --asset-qa-mode "$ASSET_QA_MODE" \
    || echo "  ⚠️ outputs_by_step 索引更新失敗"
}

organize_outdoor_placement_review () {
  [ "${ORGANIZE_OUTDOOR_PLACEMENT:-1}" = 1 ] || return 0
  if ! "$PY" - "$W" <<'PY'
import sys
from pathlib import Path

run_root = Path(sys.argv[1])

def any_existing(rel_paths: list[str]) -> bool:
    return any((run_root / rel).is_file() for rel in rel_paths)

if not any_existing([
    "placement_review/placement_preview.png",
    "placement_review/final/placement_preview.png",
    "3_composer/final_placeholder_placement.png",
]):
    raise SystemExit(1)

if not any_existing([
    "render/view.png",
    "render/connectivity_view.png",
    "populated/populated_preview.png",
    "placement_review/final/placement_preview.png",
    "placement_review/placement_preview.png",
]):
    raise SystemExit(1)

raise SystemExit(0)
PY
  then
    echo "  ↷ outdoor placement review skipped (incomplete render artifacts)"
    return 0
  fi
  local dest_root
  local review_day
  review_day=${BI_REVIEW_DAY:-$(date +%m%d)}
  dest_root=${OUTDOOR_PLACEMENT_REVIEW_ROOT:-$ROOT/output/$review_day/outdoor}
  "$PY" pipeline/organize_outdoor_placement_output.py \
      --run-root "$W" --dest "$dest_root/$CASE" \
    || echo "  ⚠️ outdoor placement review 整理失敗"
}

finish_run () {
  local rc=$?
  local status=complete
  [ "$rc" -eq 0 ] || status=failed
  record_current_stage "$status"
  printf 'total\tall requested stages\t%s\t%s\t%s\t%s\n' \
      "$RUN_STARTED_ISO" "$(date --iso-8601=seconds)" \
      "$(( $(date +%s) - RUN_STARTED_EPOCH ))" "$status" >> "$TIMING_FILE"
  organize_outputs
  organize_outdoor_placement_review
  return "$rc"
}
trap finish_run EXIT

# fine_terrain 不是只吃 scene.blend：至少還需要 coarse 的 tag 與 on-the-fly
# terrain metadata。assets 用絕對 symlink，避免複製可能很大的 terrain cache。
link_stage_metadata () {
  local src=$1 dst=$2 f
  mkdir -p "$dst"
  for f in MaskTag.json version.txt; do
    [ -f "$src/$f" ] || die "final contract 缺 metadata: $src/$f"
    # placer 會把新增資產的 tag 寫進 populated/MaskTag.json；不可拿舊 coarse
    # registry 蓋掉。其他 stage 沒有輸出時才從來源補上。
    [ ! -f "$dst/$f" ] || continue
    cp "$src/$f" "$dst/$f" || die "複製 $f 失敗"
  done
  [ -f "$src/assets/info.pickle" ] || die "final contract 缺 $src/assets/info.pickle"
  if [ -e "$dst/assets" ] && [ ! -L "$dst/assets" ]; then
    die "$dst/assets 已存在且不是 symlink，不覆寫；請先確認該 stage 目錄"
  fi
  ln -sfn "$src/assets" "$dst/assets" || die "連結 assets metadata 失敗"
  for f in pipeline_coarse.csv polycounts.txt; do
    [ ! -f "$src/$f" ] || cp "$src/$f" "$dst/$f"
  done
}

invalidate_render_gate () {
  # 只移除這個 run 生成、可隨時重建的小 marker；保留所有 blend 與 contract JSON。
  rm -f "$W/final_contract.ok"
}

if [ ! -s "$INPUT_DIR/prompt.txt" ] && [ -n "${E2E_PROMPT:-}" ]; then
  mkdir -p "$INPUT_DIR"
  printf '%s\n' "$E2E_PROMPT" > "$INPUT_DIR/prompt.txt" \
    || die "寫入 E2E_PROMPT 失敗: $INPUT_DIR/prompt.txt"
fi
[ -s "$INPUT_DIR/prompt.txt" ] || die "找不到 prompt: $INPUT_DIR/prompt.txt"
PROMPT=$(cat "$INPUT_DIR/prompt.txt")
echo "case=$CASE  prompt=$PROMPT"

# ★★明確指定 biome + seed：完全跳過挑場景★★(2026-09-08)
#   SCENE_CHOICE=forest SCENE_SEED=0 bash pipeline/run_e2e.sh <case>
#   等價於自己拼 SCENE_DIR=$ROOT/scenes/forest/seed0，但不必記路徑在哪一層。
#   ⚠️ 原本只給 SCENE_DIR 是★半套★：它只跳過 baseline picker，接著仍會掉進
#      else 去跑 pick_scene.py，而 $SCENE(step 10 餵給 generate_nature 的 -g)
#      是從那個 picker 的結果拿的 ⇒ 可能與你指定的 SCENE_DIR 不同 biome。
if [ -n "${SCENE_CHOICE:-}" ] && [ -z "${SCENE_DIR:-}" ] && [ -n "${SCENE_SEED:-}" ]; then
  for _sroot in "$ROOT/scenes" "$ROOT/scenes_inside"; do
    if [ -d "$_sroot/$SCENE_CHOICE/seed$SCENE_SEED" ]; then
      SCENE_DIR=$_sroot/$SCENE_CHOICE/seed$SCENE_SEED
      break
    fi
  done
  [ -n "${SCENE_DIR:-}" ] || \
    die "找不到場景目錄 scenes[_inside]/$SCENE_CHOICE/seed$SCENE_SEED"
fi
# 只給 SCENE_DIR ⇒ 從路徑倒推 biome 名，讓 $SCENE 與場景一致
if [ -n "${SCENE_DIR:-}" ] && [ -z "${SCENE_CHOICE:-}" ]; then
  _sd=${SCENE_DIR%/}; _sparent=${_sd%/*}; SCENE_CHOICE=${_sparent##*/}
  echo "  ℹ️ 由 SCENE_DIR 推出 biome = $SCENE_CHOICE"
fi

# ── 1 pick_scene / pick_baseline ──────────────────────────────────────────
if [ "$FROM" -le 1 ]; then
  step 1 "pick baseline scene"
  if [ -n "${SCENE_CHOICE:-}" ] && \
     { [ "$PLACEMENT_ONLY_E2E" = 1 ] || [ -n "${SCENE_DIR:-}" ]; }; then
    mkdir -p "$W/pick"
    "$PY" -c \
      'import json,sys; scene=sys.argv[1]; indoor={"Bathroom","Bedroom","DiningRoom","Kitchen","LivingRoom"}; json.dump({"scene":scene,"mode":"fixed_benchmark_scene","location":"indoor" if scene in indoor else "outdoor"},open(sys.argv[2],"w"),indent=2)' \
      "$SCENE_CHOICE" "$W/pick/pick.json" || die "寫入 fixed pick.json 失敗"
    echo "  ★固定場景★ $SCENE_CHOICE${SCENE_DIR:+ @ $SCENE_DIR}"
    echo "     不呼叫 Scene Picker / seed 挑選"
  elif [ "$E2E_BASELINE_PICKER" = 1 ] && [ -z "${SCENE_CHOICE:-}" ] && [ -z "${SCENE_DIR:-}" ]; then
    "$PY" tools/scene_picker/pick_baseline.py "$PROMPT" --out-dir "$W/pick" \
      > "$L"_pick.log 2>&1 || { tail -30 "$L"_pick.log; die "pick_baseline rc=$?"; }
    if ! "$PY" - "$W/pick/baseline_pick.json" "$W/pick/pick.json" <<'PY'
import json
import sys

src = json.load(open(sys.argv[1], encoding="utf-8"))
scene_pick = src["scene_pick"]
selected = src["selected"]
seed_pick = src["seed_pick"]
record = {
    "prompt": src.get("prompt"),
    "mode": "baseline_picker",
    "location": scene_pick.get("location"),
    "scene": selected.get("scene") or scene_pick.get("scene"),
    "seed": selected.get("seed"),
    "candidate_id": selected.get("candidate_id"),
    "scene_dir": selected.get("scene_dir"),
    "coarse_blend": selected.get("coarse_blend"),
    "baseline_blend": selected.get("baseline_blend"),
    "preview_image": selected.get("preview_image"),
    "camera_choice": selected.get("camera_choice"),
    "score": seed_pick.get("score"),
    "reason": seed_pick.get("reason"),
    "gap": seed_pick.get("visual_gap"),
    "scene_picks": src.get("scene_picks", []),
}
json.dump(record, open(sys.argv[2], "w", encoding="utf-8"), indent=2, ensure_ascii=False)
open(sys.argv[2], "a", encoding="utf-8").write("\n")
PY
    then
      die "寫入 baseline picker 相容 pick.json 失敗"
    fi
    "$PY" - "$W/pick/pick.json" <<'PY'
import json
import sys

p = json.load(open(sys.argv[1], encoding="utf-8"))
groups = ", ".join(
    f"{g.get('rank')}:{g.get('scene')}({g.get('match')}%)"
    for g in p.get("scene_picks", [])
)
print(f"  baseline picker: {groups}")
print(f"  selected baseline: {p.get('candidate_id')} score={p.get('score')} dir={p.get('scene_dir')}")
PY
  else
    "$PY" tools/scene_picker/pick_scene.py "$PROMPT" --out-dir "$W/pick" --dry-run > "$L"_pick.log 2>&1 \
      || die "pick_scene rc=$?"
  fi
fi
[ -s "$W/pick/pick.json" ] || die "缺 scene picker 結果: $W/pick/pick.json"
PICKED_SCENE=$("$PY" -c "import json;print(json.load(open('$W/pick/pick.json'))['scene'])")
SCENE_LOCATION=$("$PY" -c "import json;print(json.load(open('$W/pick/pick.json')).get('location','outdoor'))")
BASELINE_SCENE_DIR=
BASELINE_BLEND=
BASELINE_PREVIEW=
BASELINE_CAMERA_CHOICE=
if [ -s "$W/pick/baseline_pick.json" ]; then
  BASELINE_INFO=$("$PY" - "$W/pick/baseline_pick.json" <<'PY'
import json
import sys

d = json.load(open(sys.argv[1], encoding="utf-8"))
s = d.get("selected", {})
print("\t".join(str(s.get(k) or "") for k in (
    "scene_dir", "baseline_blend", "preview_image", "camera_choice"
)))
PY
)
  IFS=$'\t' read -r BASELINE_SCENE_DIR BASELINE_BLEND BASELINE_PREVIEW BASELINE_CAMERA_CHOICE <<< "$BASELINE_INFO"
fi
SCENE=${SCENE_CHOICE:-$PICKED_SCENE}
if [ -n "${SCENE_CHOICE:-}" ] && [ "$SCENE" != "$PICKED_SCENE" ]; then
  echo "  ℹ️ scene picker 建議 $PICKED_SCENE；本輪明確指定 SCENE_CHOICE=$SCENE"
fi
echo "  場景 = $SCENE"
if [ -z "${SCENE_DIR:-}" ] && [ -n "$BASELINE_SCENE_DIR" ]; then
  SCENE_DIR=$BASELINE_SCENE_DIR
fi
SCENE_DIR=${SCENE_DIR:-$ROOT/scenes/$SCENE/seed0}
case "$SCENE_DIR" in
  *"/scenes_inside/"*|scenes_inside/*) SCENE_LOCATION=indoor ;;
  *"/scenes/"*|scenes/*) SCENE_LOCATION=outdoor ;;
  *)
    scene_key=${SCENE,,}
    scene_key=${scene_key//-/}
    scene_key=${scene_key//_/}
    case "$scene_key" in
      arctic|canyon|cave|cliff|coast|coralreef|desert|forest|kelpforest|mountain|plain|river|snowymountain|underwater)
        SCENE_LOCATION=outdoor
        ;;
      bedroom|livingroom|kitchen|bathroom|diningroom|office|garage|balcony|closet|utility|staircaseroom)
        SCENE_LOCATION=indoor
        ;;
    esac
    ;;
esac
if [ -z "$SEED" ]; then
  SCENE_SEED_DIR=${SCENE_DIR%/}
  SCENE_SEED_DIR=${SCENE_SEED_DIR##*/}
  case "$SCENE_SEED_DIR" in
    seed[0-9a-fA-F]*) SEED=${SCENE_SEED_DIR#seed} ;;
    *) SEED=0 ;;
  esac
fi
echo "  scene seed = $SEED (scene dir = $SCENE_DIR)"
SB=$SCENE_DIR/coarse/scene.blend
FB=$SCENE_DIR/fine/scene.blend
# ★fine/ 被 SUPERSEDED 時改用重跑過的 fine_cam/★(2026-10-20)：否則 before 圖/影片會退回 coarse(沒長東西)
[ -s "$FB" ] || { [ -s "$SCENE_DIR/fine_cam/scene.blend" ] && FB=$SCENE_DIR/fine_cam/scene.blend; }
if [ -n "$BASELINE_BLEND" ] && [ -s "$BASELINE_BLEND" ]; then
  FB=$BASELINE_BLEND
fi
if [ "$SCENE_LOCATION" = "indoor" ]; then
  STOP_AFTER_POPULATE=1
  echo "  indoor baseline detected; stopping after populate"
fi

# ── 2 prepare ─────────────────────────────────────────────────────────────
if [ "$FROM" -le 2 ]; then
  step 2 prepare
  B=${BASELINE_BLEND:-$FB}
  [ -s "$B" ] || B=$FB
  [ -s "$B" ] || B=$SB
  [ -f "$B" ] || die "找不到 $SCENE 的 blend"
  mkdir -p "$INPUT_DIR"
  ln -sfn "$B" "$INPUT_DIR/scene.blend"
  # director 要一張場景圖給視覺模型讀
  if [ -n "$BASELINE_PREVIEW" ] && [ -s "$BASELINE_PREVIEW" ]; then
    ln -sfn "$BASELINE_PREVIEW" "$INPUT_DIR/render.png"
  else
    [ -f "$INPUT_DIR/render.png" ] || \
    cp "$(ls "$SCENE_DIR"/frames/*.png 2>/dev/null | head -1)" \
       "$INPUT_DIR/render.png" 2>/dev/null
  fi
  if [ -n "$BASELINE_SCENE_DIR" ]; then
    if [ -n "$BASELINE_CAMERA_CHOICE" ] && [ -s "$BASELINE_CAMERA_CHOICE" ]; then
      ln -sfn "$BASELINE_CAMERA_CHOICE" "$INPUT_DIR/camera_choice.json"
    elif [ -z "${CAMERA_CHOICE:-}" ]; then
      rm -f "$INPUT_DIR/camera_choice.json"
    fi
  fi
  echo "  blend → $B"
  [ ! -s "$W/pick/baseline_pick.json" ] || echo "  baseline pick → $W/pick/baseline_pick.json"
fi

if [ "$STOP_AFTER_PREPARE" = 1 ]; then
  [ -s "$INPUT_DIR/scene.blend" ] || die "STOP_AFTER_PREPARE=1 但 input scene 不存在"
  [ -s "$INPUT_DIR/render.png" ] || die "STOP_AFTER_PREPARE=1 但 input render 不存在"
  echo
  echo "★★ E2E_PREPARE_DONE  $CASE → $W ★★"
  exit 0
fi

# ── 3 指定相機優先；未指定則使用官方 active camera ─────────────────────
BAKED=$W/0_camera/baked_coarse.blend
if [ "$FROM" -le 3 ]; then
  step 3 "resolve one camera + scan_scene"
  [ -s "$SB" ] || die "找不到 coarse blend: $SB"
  mkdir -p "$W/0_camera"
  CHOICE=${CAMERA_CHOICE:-}
  if [ -z "$CHOICE" ] && [ -s "$INPUT_DIR/camera_choice.json" ]; then
    CHOICE=$INPUT_DIR/camera_choice.json
  fi
  if [ -z "$CHOICE" ] && [ -s "$SCENE_DIR/camera_choice.json" ]; then
    CHOICE=$SCENE_DIR/camera_choice.json
  fi

  if [ -n "$CHOICE" ]; then
    [ -s "$CHOICE" ] || die "指定的 CAMERA_CHOICE 不存在: $CHOICE"
    cp "$CHOICE" "$W/0_camera/camera_choice.json"
    CUDA_VISIBLE_DEVICES=${GPUS%%,*} "$PY" tools/photographer/bake_camera.py -- \
        "$SB" "$BAKED" "$W/0_camera/camera_choice.json" > "$L"_bake.log 2>&1 \
      || { tail -20 "$L"_bake.log; die "bake_camera rc=$?"; }
    VISUAL_BLEND=$BAKED
    # Placement-only 只檢查 coarse placeholder。載入、bake、render 數百 MB 的
    # populated fine scene 不增加任何 placement 訊息，反而容易吃掉整張 GPU。
    if [ "$PLACEMENT_ONLY_E2E" != 1 ] && [ -s "$FB" ]; then
      VISUAL_BLEND=$W/0_camera/baked_visual.blend
      CUDA_VISIBLE_DEVICES=${GPUS%%,*} "$PY" tools/photographer/bake_camera.py -- \
          "$FB" "$VISUAL_BLEND" "$W/0_camera/camera_choice.json" \
          > "$L"_bake_visual.log 2>&1 \
        || { tail -20 "$L"_bake_visual.log; die "visual camera bake rc=$?"; }
    fi
    CAMERA_VIEW=$W/0_camera/selected.png
    CUDA_VISIBLE_DEVICES=${GPUS%%,*} PYTHONPATH="$ROOT/core:$INF" "$PY" stage_exec/render_view.py -- \
        "$VISUAL_BLEND" "$CAMERA_VIEW" --as-is --frame 1 --res 960 540 --samples 32 \
        > "$L"_selected_camera.log 2>&1 \
      || { tail -20 "$L"_selected_camera.log; die "指定 camera preview 失敗"; }
    echo "  ★相機 = 指定設定 $CHOICE（placement-only 使用 baked coarse）★"
  else
    ln -sfn "$SB" "$BAKED"
    "$PY" -c "
import json
from pathlib import Path
Path('$W/0_camera/camera_choice.json').write_text(json.dumps({
  'mode': 'infinigen_official', 'angle': 'ground', 'blend': '$SB',
  'why': 'Use the original active camera and preserve its animation.'
}, indent=2) + '\\n')"
    CAMERA_VIEW=$SCENE_DIR/frames/frame_0001.png
    if [ ! -s "$CAMERA_VIEW" ]; then
      CAMERA_VIEW=$W/0_camera/official.png
      VISUAL_BLEND=$FB
      [ -s "$VISUAL_BLEND" ] || VISUAL_BLEND=$SB
      CUDA_VISIBLE_DEVICES=${GPUS%%,*} PYTHONPATH="$ROOT/core:$INF" "$PY" stage_exec/render_view.py -- \
          "$VISUAL_BLEND" "$CAMERA_VIEW" --as-is --frame 1 --res 960 540 --samples 32 \
          > "$L"_official_camera.log 2>&1 \
        || { tail -20 "$L"_official_camera.log; die "官方 camera preview 失敗"; }
    fi
    echo "  ★相機 = Infinigen 官方 active camera★"
  fi
  rm -f "$INPUT_DIR/render.png"
  cp "$CAMERA_VIEW" "$INPUT_DIR/render.png"
  echo "  director 將看 $CAMERA_VIEW"

  CUDA_VISIBLE_DEVICES=${GPUS%%,*} PYTHONPATH="$ROOT/core:$INF" "$PY" director/scan_scene.py -- \
      "$BAKED" "$W/scene_manifest.json" --scene "$SCENE" > "$L"_scan.log 2>&1 \
    || die "scan_scene rc=$?"
  "$PY" -c "
import json;m=json.load(open('$W/scene_manifest.json'))
print('  collections:',[c['collection'] for c in m.get('collections',[])])
print('  scatters   :',[s['label'].split('(')[0] for s in m.get('scatters',[])][:8])"
fi

# ── 4 director ────────────────────────────────────────────────────────────
if [ "$FROM" -le 4 ]; then
  step 4 director
  mkdir -p "$W/1_director"
  if [ -n "${E2E_DIRECTOR_PLAN_SCRIPT:-}" ]; then
    "$PY" "$E2E_DIRECTOR_PLAN_SCRIPT" "$W/1_director/edit_plan.json" \
        "$INPUT_DIR" "$W/scene_manifest.json" "$SCENE" \
        > "$L"_director.log 2>&1 || die "director plan hook rc=$?"
  else
    LIB_ARG=(--no-library)
    [ "$BI_LIBRARY" != 1 ] || LIB_ARG=(--library)
    "$PY" director/director.py "$INPUT_DIR" --project "$CASE" \
        --manifest "$W/scene_manifest.json" -o "$W/1_director" "${LIB_ARG[@]}" \
        > "$L"_director.log 2>&1 || die "director rc=$?"
  fi
  sed -n '/EDIT PLAN/,$p' "$L"_director.log | head -16
  if [ -n "${E2E_EDIT_PLAN_PATCH_SCRIPT:-}" ]; then
    "$PY" "$E2E_EDIT_PLAN_PATCH_SCRIPT" "$W/1_director/edit_plan.json" \
      || die "edit-plan patch hook rc=$?"
  fi
fi

# Director 已經決定 world_motion；在任何資產生成/API/GPU 工作前檢查 frame
# contract。Placement-only Step 8 不 author motion，故不受此 gate 影響。
if [ "$PLACEMENT_ONLY_E2E" != 1 ] && [ "$STOP_AFTER_PLACEMENT_REVIEW" != 1 ]; then
  MOTION_CONTRACT_RC=0
  PYTHONPATH="$ROOT/core:$ROOT" "$PY" stage_exec/check_world_motion_frames.py \
    --plan "$W/1_director/edit_plan.json" \
    --frame-start "$FRAME_START" --frame-end "$FRAME_END" \
    || MOTION_CONTRACT_RC=$?
  if [ "$MOTION_CONTRACT_RC" -ne 0 ]; then
    if [ "$CONNECTIVITY_SMOKE" = 1 ]; then
      echo "  ⚠️ connectivity-only: world-motion frame quality FAIL retained"
    else
      soft_die "world-motion frame contract FAIL（尚未啟動 asset generation）"
    fi
  fi
fi

if [ "$STOP_AFTER_DIRECTOR" = 1 ]; then
  [ -s "$W/1_director/edit_plan.json" ] || die "STOP_AFTER_DIRECTOR=1 但 edit_plan.json 不存在"
  organize_outputs
  echo
  echo "★★ E2E_DIRECTOR_DONE  $CASE → $W/1_director/edit_plan.json ★★"
  echo "   (未執行 asset generation / overlay / composer / placer / fine / render)"
  exit 0
fi

# ── 5 ★階段 B★ 生資產 ─────────────────────────────────────────────────────
if [ "$FROM" -le 5 ]; then
  if [ "$PLACEMENT_ONLY_E2E" = 1 ]; then
    echo "  ⏭️  placement-only：跳過 Step 5 asset generation"
  else
    step 5 "genlist + 資產生成(★這一段以前從沒被自動叫過★)"
  "$PY" pipeline/glue.py genlist --plan "$W/1_director/edit_plan.json" \
      --out "$W/2_director/generation_list.json" || die "genlist 失敗"
  # ADAPT：把庫存基底 materialize 出來，generator 才知道要從既有變體出發改。
  # 沒有 ADAPT 項目時這一步是 no-op。
  if [ "$BI_LIBRARY" = 1 ]; then
    "$PY" pipeline/glue.py prepare-adapt \
        --genlist "$W/2_director/generation_list.json" --scene-root "$W" \
      || die "prepare-adapt 失敗"
  fi
  GEN_OBJECT_COUNT=$("$PY" - "$W/2_director/generation_list.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
print(len(data.get("objects") or []))
PY
  ) || die "讀取 generation_list objects 數量失敗"
  # ★SKIP_GEN=1★: 只產 generation_list，★不叫生成器★(2026-08-09)
  #   驗 pipeline 時每一輪都重生資產是 2×30~50 分鐘 + LLM 錢，而生成那一段
  #   0806 已經驗過。正式重用資產改從 output/asset/ 查找，不再要求
  #   3_procedural/<id> 或 3_blender/<id> symlink 進 $W。
  if [ "${SKIP_GEN:-0}" = "1" ] || [ "$GEN_OBJECT_COUNT" = "0" ]; then
    printf 'generator_skipped=true\nreason=%s\n' \
      "$([ "$GEN_OBJECT_COUNT" = "0" ] && printf 'empty_generation_list' || printf 'SKIP_GEN')" \
      > "$W/2_director/generation_skipped.flag"
    if [ "$GEN_OBJECT_COUNT" = "0" ]; then
      echo "  ⏭️  generation_list.objects=0 ⇒ 全部資產走 library reuse，跳過 3_generator.py"
    else
      echo "  ⏭️  SKIP_GEN=1 ⇒ 跳過生成器。overlay 會從 output/asset/ 嘗試重用"
    fi
  else
    echo "  → 3_generator.py 開始(GPU $GPUS，asset QA=$ASSET_QA_MODE，procedural-only)"
    GEN_RENDER_ARGS=(
      --turntable-frames-per-view "$ASSET_TURNTABLE_FRAMES_PER_VIEW"
      --turntable-views "$ASSET_TURNTABLE_VIEWS")
    [ "$ASSET_TURNTABLE" = 1 ] || GEN_RENDER_ARGS+=(--no-turntable)
    [ "$ASSET_SEED_CHECK" = 1 ] || GEN_RENDER_ARGS+=(--no-seed-check)
    ( cd "$EW" && "$PY" 3_generator.py "$W" --gpus "$GPUS" \
        --asset-qa-mode "$ASSET_QA_MODE" "${GEN_RENDER_ARGS[@]}" ) > "$L"_genassets.log 2>&1
    rc=$?
    mkdir -p "$ROOT/output/asset/_manifests"
    cp "$L"_genassets.log "$ROOT/output/asset/_manifests/${CASE}_genassets.log" 2>/dev/null || true
    echo "  3_generator rc=$rc  (詳見 ${L}_genassets.log)"
    grep -aE "^\[|DONE|FAIL" "$L"_genassets.log | tail -12
    if [ $rc -ne 0 ]; then
    tail -30 "$L"_genassets.log
    GEN_OK=$(grep -a -o -E "ok=[0-9]+" "$L"_genassets.log | tail -1 | tr -dc 0-9)
    if [ "$BI_SOFT_GATES" = 1 ] && [ "${GEN_OK:-0}" -ge 1 ]; then
      echo "  ⚠️ (soft gate) 部分資產生成失敗(ok=$GEN_OK)，缺的資產改用官方外觀繼續 —— STRICT=0"
    else
      die "asset generation 失敗"
    fi
  fi
  fi
  fi
fi

# ── 6 合併 overlay ────────────────────────────────────────────────────────
if [ "$FROM" -le 6 ]; then
  if [ "$PLACEMENT_ONLY_E2E" = 1 ]; then
    echo "  ⏭️  placement-only：跳過 Step 6 overlay merge"
  else
    step 6 "staged overlay roots(多資產同場)"
  REUSE_ARG=()
  if [ "${SKIP_GEN:-0}" = "1" ] || \
     [ -f "$W/2_director/generation_skipped.flag" ]; then
    REUSE_ARG=(--reuse-existing)
  fi
  "$PY" pipeline/glue.py overlays --genlist "$W/2_director/generation_list.json" \
      --scene-root "$W" --out "$W/overlay_merged" --table "$W/overlay_table.json" \
      "${REUSE_ARG[@]}" \
    || die "overlay 合併失敗"
  fi
fi

# ── 7 composer ────────────────────────────────────────────────────────────
if [ "$FROM" -le 7 ]; then
  step 7 composer
  PLACEMENT_ONLY_E2E="$PLACEMENT_ONLY_E2E" \
  "$PY" planner/composer.py --plan "$W/1_director/edit_plan.json" \
      --manifest "$W/scene_manifest.json" --out-dir "$W/3_composer" \
      > "$L"_composer.log 2>&1 || die "composer rc=$?"
  grep -aE "確定性檢查|⛔|→ " "$L"_composer.log | tail -6
fi

# ── 8 ★階段 A★ apply_plan + 回填 code ─────────────────────────────────────
if [ "$FROM" -le 8 ]; then
  step 8 "apply_plan + placeholder preview/review + 回填 code"
  mkdir -p "$W/work"
  # Stage 8 rebuilds work/scene.blend from the immutable baked camera source.
  # Copying a multi-GB coarse scene here only to delete it immediately is both
  # slow and unnecessary; carry over every metadata entry except scene.blend.
  for coarse_entry in "$SCENE_DIR/coarse/"*; do
    [ "${coarse_entry##*/}" = scene.blend ] && continue
    cp -r "$coarse_entry" "$W/work"/ 2>/dev/null
  done
  # ★吃 baked blend，且不傳任何相機參數★ —— 相機已經在 blend 裡，
  #   apply_plan 讀 scene.camera 就是對的那台。
  mkdir -p "$W/placement_review"
  # ★extra_edits(ground_scatter) 的 code 必須在 apply_plan 之前解析成真實路徑★
  #   fillcode 只管 manifest 的 HERO placements，而且是跑在 apply_plan 之後；
  #   composer 寫的 "generated/<slug>" 若沒換掉，load_overlay_factory 會找不到
  #   factory.txt。對不上就降級成官方外觀，不讓壞路徑弄掛整個 step 8。
  if [ "$PLACEMENT_ONLY_E2E" != 1 ] && [ -s "$W/overlay_table.json" ]; then
    "$PY" pipeline/glue.py fillextracode \
        --plan "$W/3_composer/edit_plan.json" --table "$W/overlay_table.json" \
        --director-plan "$W/1_director/edit_plan.json" \
      || echo "  ⚠️ extra_edits code 解析失敗（scatter 將使用官方外觀）"
  fi
  echo "  NO_CANDIDATE_SELECTOR_LOOP: formal Step 8 uses one-shot placement only"
  MAP_FIRST_ACTIVE=0
  INITIAL_MAP_DIR="$W/placement_review/region_map_initial"
  IMAGE_PLACEMENT_DIR="$W/placement_review/composer_image_initial"
  IMAGE_REGION_MAP_INDEX=
  LAST_MAP_INDEX=
  LAST_APPLICATION=
  LAST_SELECTION=
  LAST_ATTEMPT_DIR=
  # ★2026-10-19 起預設 1★：output/final 的成品(0912~0915 各批 _common.sh)全是 BI_HERO_SIMPLE=1；
  #   預設 0 的嚴格 region map 在 1019_text_sub 讓熱氣球/飛鳥/斜坡上的猴子整個 case 判死。
  #   BI_HERO_SIMPLE=0 可退回嚴格 region map。
  if [ "${BI_HERO_SIMPLE:-1}" = 1 ]; then
    # ★B 方案(2026-09-12 起；2026-09-13 擴及地面)★：hero 用三條規則的簡單階梯放，
    #   不走 region map。小的靠近、大的推遠到不擋鏡頭、放看得到的空位；
    #   塞不下放側邊，不報錯。
    #   ⚠️ ★範圍不是只有空中/水下★ —— simple_volume_placement.py 的 targets 是
    #      「volume(airborne/submerged) ★或★ surface(conform/upright/tilted/rooted/
    #      afloat)」的 hero add，也就是★所有 map-managed 的 hero add★。地面物件
    #      用同一套規則，只是落點來自格點相機射線打到的表面、且照 surface_policy
    #      篩表面種類與坡度。(0912 這裡原本寫「地面的照舊走 region map」，
    #      09-13 擴充後已不成立。)
    #   放好的 edit 標 placement_route=simple_volume，下面的 region map 會略過它們；
    #   region map 路徑完整保留，給沒有被這支認領的項目使用。
    SIMPLE_DIR="$W/placement_review/simple_volume"
    mkdir -p "$SIMPLE_DIR"
    cp "$W/3_composer/edit_plan.json" "$SIMPLE_DIR/edit_plan_before.json"
    echo "  BI_HERO_SIMPLE=1: surface AND volume heroes use the three-rule depth ladder (no region map)"
    CUDA_VISIBLE_DEVICES=${GPUS%%,*} \
      PYTHONPATH="$ROOT/core:$ROOT:$ROOT/stage_exec:$INF" \
      "$PY" stage_exec/simple_volume_placement.py -- \
        "$BAKED" "$W/3_composer/edit_plan.json" "$SIMPLE_DIR" "$FRAME_START" \
        --scene "$SCENE" \
        > "$SIMPLE_DIR/simple_volume_placement.log" 2>&1 \
      || { tail -30 "$SIMPLE_DIR/simple_volume_placement.log"; \
           die "simple volume placement crashed"; }
    grep "^\[simple\]" "$SIMPLE_DIR/simple_volume_placement.log" | sed 's/^/  /'
    cp "$W/3_composer/edit_plan.json" "$SIMPLE_DIR/edit_plan_after.json"
  fi
  if [ "$COMPOSER_IMAGE_PLACEMENT" = 1 ]; then
    mkdir -p "$IMAGE_PLACEMENT_DIR"
    IMAGE_PROBE_LOG="$IMAGE_PLACEMENT_DIR/image_placement_probe.log"
    PYTHONPATH="$ROOT/core:$ROOT:$INF" "$PY" - "$W/3_composer/edit_plan.json" \
      > "$IMAGE_PROBE_LOG" 2>&1 <<'PY'
import json
import sys
from placement_region_contract import all_map_managed_instances

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        result = all_map_managed_instances(json.load(handle))
except Exception as error:
    print(f"{type(error).__name__}: {error}", file=sys.stderr)
    raise SystemExit(2)
raise SystemExit(0 if result else 1)
PY
    image_probe_rc=$?
    if [ "$image_probe_rc" -eq 1 ]; then
      echo "  composer image placement: no map-managed adds; using normal placement"
    elif [ "$image_probe_rc" -ne 0 ]; then
      tail -30 "$IMAGE_PROBE_LOG"
      die "composer image placement schema invalid (rc=$image_probe_rc)"
    else
      cp "$W/3_composer/edit_plan.json" "$IMAGE_PLACEMENT_DIR/edit_plan_before.json"
      echo "  COMPOSER_IMAGE_PLACEMENT: Composer sees original render, not grid/contact sheet"
      echo "  MAP_RAYCAST_SNAP: build feasibility map only for deterministic coordinate recovery"
      [ "$COMPOSER_IMAGE_SURFACE_CHOICE" != 1 ] || \
        echo "  COMPOSER_IMAGE_SURFACE_CHOICE: Agent may choose among registered indoor support plane types"
      MAP_BUILD_EXTRA_ARGS=()
      if [ "$PLACEMENT_SEMANTIC_GRID_PREVIEW" = 1 ]; then
        MAP_BUILD_EXTRA_ARGS=(--semantic-support-grid)
      fi
      if [ "$COMPOSER_IMAGE_SURFACE_CHOICE" = 1 ]; then
        MAP_BUILD_EXTRA_ARGS+=("--indoor-semantic-surface-choice")
      fi
      if [ "$PLACEMENT_INDOOR_SURFACE_FALLBACK" = 0 ]; then
        echo "  PLACEMENT_INDOOR_SURFACE_FALLBACK=0: strict requested support types"
        MAP_BUILD_EXTRA_ARGS+=("--no-indoor-surface-fallback")
      fi
      CUDA_VISIBLE_DEVICES=${GPUS%%,*} \
        PYTHONPATH="$ROOT/core:$ROOT:$ROOT/stage_exec:$INF" \
        "$PY" stage_exec/build_placement_region_map.py -- \
          "$BAKED" "$W/3_composer/edit_plan.json" - \
          "$IMAGE_PLACEMENT_DIR" "$PLACEMENT_REGION_MAP_COLUMNS" \
          "$PLACEMENT_REGION_MAP_ROWS" "$FRAME_START" --all-map-managed \
          --scene "$SCENE" --scene-location "$SCENE_LOCATION" \
          "${MAP_BUILD_EXTRA_ARGS[@]}" \
          > "$IMAGE_PLACEMENT_DIR/region_map_build.log" 2>&1
      image_map_rc=$?
      if [ "$image_map_rc" -ne 0 ]; then
        tail -30 "$IMAGE_PLACEMENT_DIR/region_map_build.log"
        die "composer-image feasibility map infeasible (rc=$image_map_rc)"
      fi
      IMAGE_REGION_MAP_INDEX="$IMAGE_PLACEMENT_DIR/region_map_index.json"
      PYTHONPATH="$ROOT/core:$ROOT:$ROOT/stage_exec:$INF" "$PY" \
        planner/composer_image_placement_selector.py \
        --plan "$W/3_composer/edit_plan.json" \
        --scene-render "$INPUT_DIR/render.png" \
        --manifest "$W/scene_manifest.json" \
        --map-index "$IMAGE_REGION_MAP_INDEX" \
        --output "$IMAGE_PLACEMENT_DIR/composer_image_selection.json" \
        --application "$IMAGE_PLACEMENT_DIR/composer_image_application.json" \
        --plan-output "$IMAGE_PLACEMENT_DIR/edit_plan_after.json" \
        --apply \
        > "$IMAGE_PLACEMENT_DIR/composer_image_selection.log" 2>&1 \
        || { tail -30 "$IMAGE_PLACEMENT_DIR/composer_image_selection.log"; \
             die "Composer image placement selection failed"; }
      mkdir -p "$IMAGE_PLACEMENT_DIR/selected_maps"
      PYTHONPATH="$ROOT/core:$ROOT:$ROOT/stage_exec:$INF" "$PY" \
        stage_exec/annotate_placement_region_map.py \
        --map-index "$IMAGE_REGION_MAP_INDEX" \
        --application "$IMAGE_PLACEMENT_DIR/composer_image_application.json" \
        --history-root "$W/placement_review" \
        --output-dir "$IMAGE_PLACEMENT_DIR/selected_maps" \
        --label "composer image-guided placement" \
        > "$IMAGE_PLACEMENT_DIR/region_annotation.log" 2>&1 \
        || echo "  ⚠️ composer image placement map annotation failed; placement continues"
      LAST_MAP_INDEX="$IMAGE_REGION_MAP_INDEX"
      LAST_APPLICATION="$IMAGE_PLACEMENT_DIR/composer_image_application.json"
      LAST_SELECTION="$IMAGE_PLACEMENT_DIR/composer_image_selection.json"
      LAST_ATTEMPT_DIR="$IMAGE_PLACEMENT_DIR"
    fi
  elif [ "$MACHINE_COMPOSER_REGION_MAP" = 1 ]; then
    mkdir -p "$INITIAL_MAP_DIR"
    MAP_FIRST_PROBE_LOG="$INITIAL_MAP_DIR/map_first_probe.log"
    PYTHONPATH="$ROOT/core:$ROOT:$INF" "$PY" - "$W/3_composer/edit_plan.json" \
      > "$MAP_FIRST_PROBE_LOG" 2>&1 <<'PY'
import json
import sys
from placement_region_contract import all_map_managed_instances

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        result = all_map_managed_instances(json.load(handle))
except Exception as error:
    print(f"{type(error).__name__}: {error}", file=sys.stderr)
    raise SystemExit(2)
raise SystemExit(0 if result else 1)
PY
    map_first_probe_rc=$?
    if [ "$map_first_probe_rc" -eq 0 ]; then
    MAP_FIRST_ACTIVE=1
    mkdir -p "$INITIAL_MAP_DIR/selected_maps"
    cp "$W/3_composer/edit_plan.json" "$INITIAL_MAP_DIR/edit_plan_before.json"
    echo "  MAP_FIRST_DETERMINISTIC_MODE: build feasibility map before initial placement"
    echo "  NO_PLACEMENT_AGENT_CALL: arrangement-aware deterministic green-cell selection"
    MAP_BUILD_EXTRA_ARGS=()
    if [ "$PLACEMENT_SEMANTIC_GRID_PREVIEW" = 1 ]; then
      MAP_BUILD_EXTRA_ARGS=(--semantic-support-grid)
    fi
    if [ "$PLACEMENT_INDOOR_SURFACE_FALLBACK" = 0 ]; then
      echo "  PLACEMENT_INDOOR_SURFACE_FALLBACK=0: strict requested support types"
      MAP_BUILD_EXTRA_ARGS+=("--no-indoor-surface-fallback")
    fi
    CUDA_VISIBLE_DEVICES=${GPUS%%,*} \
      PYTHONPATH="$ROOT/core:$ROOT:$ROOT/stage_exec:$INF" \
      "$PY" stage_exec/build_placement_region_map.py -- \
        "$BAKED" "$W/3_composer/edit_plan.json" - \
        "$INITIAL_MAP_DIR" "$PLACEMENT_REGION_MAP_COLUMNS" \
        "$PLACEMENT_REGION_MAP_ROWS" "$FRAME_START" --all-map-managed \
        --scene "$SCENE" --scene-location "$SCENE_LOCATION" \
        "${MAP_BUILD_EXTRA_ARGS[@]}" \
        > "$INITIAL_MAP_DIR/region_map_build.log" 2>&1
    initial_map_rc=$?
    if [ "$initial_map_rc" -ne 0 ]; then
      tail -30 "$INITIAL_MAP_DIR/region_map_build.log"
      if [ "$CONNECTIVITY_SMOKE" = 1 ]; then
        # A zero-capacity map is still valuable connectivity evidence.  Formal
        # runs must stop, but a connectivity smoke must retain the map and keep
        # exercising apply/populate/fine/render.  Fall back to the Composer's
        # submitted coordinates; the normal machine-review path below will
        # reject them honestly and promote only its explicitly unqualified
        # diagnostic placeholder scene.
        MAP_FIRST_ACTIVE=0
        echo "  ⚠️ connectivity-only: initial region map infeasible; evidence retained"
        echo "  ⚠️ connectivity-only: falling back to unqualified diagnostic placement"
      else
        die "initial deterministic region map infeasible (rc=$initial_map_rc)"
      fi
    fi
    if [ "$MAP_FIRST_ACTIVE" = 1 ]; then
      PYTHONPATH="$ROOT/core:$ROOT:$INF" "$PY" \
        stage_exec/apply_placement_region_selection.py \
        --plan "$W/3_composer/edit_plan.json" \
        --map-index "$INITIAL_MAP_DIR/region_map_index.json" \
        --auto-select --all-map-managed \
        --selection-output "$INITIAL_MAP_DIR/region_selection.json" \
        --output "$INITIAL_MAP_DIR/edit_plan_after.json" \
        --application "$INITIAL_MAP_DIR/region_application.json" \
        --infeasibility-output "$INITIAL_MAP_DIR/region_selection_infeasible.json" \
        > "$INITIAL_MAP_DIR/region_application.log" 2>&1 \
        || { tail -30 "$INITIAL_MAP_DIR/region_application.log"; \
             die "initial deterministic region selection infeasible"; }
      PYTHONPATH="$ROOT/core:$ROOT:$ROOT/stage_exec:$INF" "$PY" \
        stage_exec/annotate_placement_region_map.py \
        --map-index "$INITIAL_MAP_DIR/region_map_index.json" \
        --application "$INITIAL_MAP_DIR/region_application.json" \
        --history-root "$W/placement_review" \
        --output-dir "$INITIAL_MAP_DIR/selected_maps" \
        --label "initial deterministic selection" \
        > "$INITIAL_MAP_DIR/region_annotation.log" 2>&1 \
        || { tail -30 "$INITIAL_MAP_DIR/region_annotation.log"; \
             die "initial region-map annotation failed"; }
      LAST_MAP_INDEX="$INITIAL_MAP_DIR/region_map_index.json"
      LAST_APPLICATION="$INITIAL_MAP_DIR/region_application.json"
      LAST_SELECTION="$INITIAL_MAP_DIR/region_selection.json"
      LAST_ATTEMPT_DIR="$INITIAL_MAP_DIR"
    fi
    elif [ "$map_first_probe_rc" -eq 1 ]; then
      echo "  map-first probe: no map-managed adds; using normal placement"
    else
      tail -30 "$MAP_FIRST_PROBE_LOG"
      die "map-first placement schema invalid (rc=$map_first_probe_rc)"
    fi
  fi
  REGION_REVIEW_ARGS=()
  REGION_MAP_INDEX_FOR_REVIEW=
  if [ "$MAP_FIRST_ACTIVE" = 1 ]; then
    REGION_MAP_INDEX_FOR_REVIEW="$INITIAL_MAP_DIR/region_map_index.json"
  elif [ -n "$IMAGE_REGION_MAP_INDEX" ]; then
    REGION_MAP_INDEX_FOR_REVIEW="$IMAGE_REGION_MAP_INDEX"
  fi
  if [ -n "$REGION_MAP_INDEX_FOR_REVIEW" ]; then
    REGION_REVIEW_ARGS=(
      --region-map-index "$REGION_MAP_INDEX_FOR_REVIEW")
  fi
  SEMANTIC_GRID_ARGS=()
  if [ "$PLACEMENT_SEMANTIC_GRID_PREVIEW" = 1 ]; then
    SEMANTIC_GRID_ARGS=(--semantic-grid-overlay)
  fi
  TOP_PREVIEW_OUT="$W/placement_review/placement_preview_top.png"
  [ "$INITIAL_PLACEMENT_TOP_PREVIEW" = 1 ] || TOP_PREVIEW_OUT=-
  INITIAL_REPORT_OUT="$W/report.json"
  INITIAL_REVIEW_OUT="$W/placement_review/placement_review.json"
  INITIAL_PREVIEW_OUT="$W/placement_review/placement_preview.png"
  INITIAL_TOP_OUT="$TOP_PREVIEW_OUT"
  if [ "$MAP_FIRST_ACTIVE" = 1 ]; then
    INITIAL_REPORT_OUT="$INITIAL_MAP_DIR/selected_report.json"
    INITIAL_REVIEW_OUT="$INITIAL_MAP_DIR/selected_machine_review.json"
    INITIAL_PREVIEW_OUT="$INITIAL_MAP_DIR/selected_placement_preview.png"
    INITIAL_TOP_OUT="$INITIAL_MAP_DIR/selected_placement_preview_top.png"
  fi
  CUDA_VISIBLE_DEVICES=${GPUS%%,*} PYTHONPATH="$ROOT/core:$INF" \
    BI_FRAME_START="$FRAME_START" BI_FRAME_END="$FRAME_END" \
    "$PY" stage_exec/run_initial_placement_batch.py -- \
      "$BAKED" "$W/work/scene.blend" \
      "$W/3_composer/edit_plan.json" "$INITIAL_REPORT_OUT" \
      "$INITIAL_REVIEW_OUT" "$INITIAL_PREVIEW_OUT" \
      "$INITIAL_TOP_OUT" "$FRAME_START" "${REGION_REVIEW_ARGS[@]}" \
      "${SEMANTIC_GRID_ARGS[@]}" \
      > "$L"_initial_placement_batch.log 2>&1 \
    || { tail -30 "$L"_initial_placement_batch.log; die "initial placement batch rc=$?"; }
  if [ "$MAP_FIRST_ACTIVE" = 1 ] && [ ! -s "$INITIAL_PREVIEW_OUT" ]; then
    "$PY" stage_exec/prepare_nonconverged_preview.py \
      "$W/3_composer/edit_plan.json" "$INITIAL_MAP_DIR/diagnostic_edit_plan.json" \
      || die "prepare initial map diagnostic plan failed"
    CUDA_VISIBLE_DEVICES=${GPUS%%,*} PYTHONPATH="$ROOT/core:$INF" \
      BI_FRAME_START="$FRAME_START" BI_FRAME_END="$FRAME_END" \
      "$PY" stage_exec/run_initial_placement_batch.py -- \
        "$BAKED" "$INITIAL_MAP_DIR/diagnostic_scene.blend" \
        "$INITIAL_MAP_DIR/diagnostic_edit_plan.json" \
        "$INITIAL_MAP_DIR/diagnostic_report.json" \
        "$INITIAL_MAP_DIR/diagnostic_review.json" \
        "$INITIAL_PREVIEW_OUT" "$INITIAL_TOP_OUT" "$FRAME_START" \
        > "$INITIAL_MAP_DIR/diagnostic_render.log" 2>&1 \
      || { tail -30 "$INITIAL_MAP_DIR/diagnostic_render.log"; \
           die "initial selected-placement diagnostic render failed"; }
  fi
  if [ "$MAP_FIRST_ACTIVE" = 1 ]; then
    cp "$INITIAL_REVIEW_OUT" "$W/placement_review/placement_review.json"
    [ ! -s "$INITIAL_REPORT_OUT" ] || cp "$INITIAL_REPORT_OUT" "$W/report.json"
    [ ! -s "$INITIAL_MAP_DIR/selected_manifest.json" ] || \
      cp "$INITIAL_MAP_DIR/selected_manifest.json" "$W/manifest.json"
    [ ! -s "$INITIAL_PREVIEW_OUT" ] || \
      cp "$INITIAL_PREVIEW_OUT" "$W/placement_review/placement_preview.png"
    [ ! -s "$INITIAL_TOP_OUT" ] || \
      cp "$INITIAL_TOP_OUT" "$W/placement_review/placement_preview_top.png"
  fi
  grep -aE "^\[plan\]|^\[hero\]|^\[manifest\]" \
    "$L"_initial_placement_batch.log | tail -8
  # Machine failures are facts, not visual judgements.  Give them back to the
  # Composer to rewrite only the failed instances' screen coordinates, then
  # rebuild from the immutable baked source.  The bound prevents an impossible
  # geometry contract or API mistake from becoming an infinite loop.
  machine_round=0
  MACHINE_REVIEW_ACCEPTED=1
  # ★SA3 ablation(2026-09-20)★：BI_ABLATION_ACCEPT_FIRST=1 ⇒ 接受第一次擺放，
  #   跳過整個 machine review 重試迴圈。★其餘 gate 全部照常★(placer contract、
  #   final-scene contract、render gate)，所以只有「驗證與重試」這一個元件被拿掉。
  #   ⚠️ 不要改用 CONNECTIVITY_SMOKE=1 —— 它會連 placer/final contract 一起繞過，
  #      那是三個變數一起動。
  #   placement_review.json ★保持原樣★(可能是 NEEDS_REVISION)，離線量測要用它。
  if [ "${BI_ABLATION_ACCEPT_FIRST:-0}" = 1 ]; then
    echo "  ★ABLATION_ACCEPT_FIRST=1：接受第一次擺放，不做 machine review 重試★"
    echo "     placement_review.json 保留原始判定供離線量測"
  fi
  while [ "${BI_ABLATION_ACCEPT_FIRST:-0}" != 1 ] && ! "$PY" -c \
      'import json,sys; raise SystemExit(0 if json.load(open(sys.argv[1]))["status"] == "PASS" else 1)' \
      "$W/placement_review/placement_review.json"; do
    if [ "$machine_round" -ge "$MACHINE_COMPOSER_REVIEW_ROUNDS" ]; then
      echo "⛔ machine review still failing after $machine_round Composer revisions"
      "$PY" -c \
        'import json,sys; [print("  -", x) for x in json.load(open(sys.argv[1])).get("failures", [])]' \
        "$W/placement_review/placement_review.json"
      # A rejected projection can stop before placeholder creation.  Preserve a
      # clearly marked visual of Composer's final proposal without changing the
      # canonical plan or treating the diagnostic boxes as accepted geometry.
      DIAGNOSTIC_PLAN="$W/placement_review/nonconverged_edit_plan.json"
      "$PY" stage_exec/prepare_nonconverged_preview.py \
        "$W/3_composer/edit_plan.json" "$DIAGNOSTIC_PLAN" \
        || die "prepare nonconverged diagnostic plan failed"
      CUDA_VISIBLE_DEVICES=${GPUS%%,*} PYTHONPATH="$ROOT/core:$INF" \
        BI_FRAME_START="$FRAME_START" BI_FRAME_END="$FRAME_END" \
        "$PY" stage_exec/run_initial_placement_batch.py -- \
          "$BAKED" "$W/placement_review/nonconverged_scene.blend" \
          "$DIAGNOSTIC_PLAN" "$W/placement_review/nonconverged_report.json" \
          "$W/placement_review/nonconverged_review.json" \
          "$W/placement_review/nonconverged_preview.png" \
          "$W/placement_review/nonconverged_preview_top.png" "$FRAME_START" \
          > "$W/placement_review/nonconverged_render.log" 2>&1 \
        || { tail -30 "$W/placement_review/nonconverged_render.log"; \
             die "nonconverged diagnostic render failed"; }
      echo "  diagnostic preview → $W/placement_review/nonconverged_preview.png"
      echo "  diagnostic top     → $W/placement_review/nonconverged_preview_top.png"
      if [ "$CONNECTIVITY_SMOKE" = 1 ] || [ "$BI_SOFT_GATES" = 1 ]; then
        # Projection rejection can happen before the canonical work scene and
        # manifest are written.  The diagnostic batch above deliberately
        # realizes every proposed placeholder, so it is a usable structural
        # input for a connectivity smoke run even though it is not a formally
        # accepted placement.  Promote that matched scene/manifest pair only
        # in connectivity mode and retain the original NEEDS_REVISION review.
        if { [ ! -s "$W/work/scene.blend" ] || [ ! -s "$W/manifest.json" ]; } && \
           [ -s "$W/placement_review/nonconverged_scene.blend" ] && \
           [ -s "$W/placement_review/nonconverged_manifest.json" ]; then
          mkdir -p "$W/work"
          cp "$W/placement_review/nonconverged_scene.blend" "$W/work/scene.blend"
          cp "$W/placement_review/nonconverged_manifest.json" "$W/manifest.json"
          [ ! -s "$W/placement_review/nonconverged_report.json" ] || \
            cp "$W/placement_review/nonconverged_report.json" "$W/report.json"
          cp "$W/placement_review/nonconverged_preview.png" \
            "$W/placement_review/placement_preview.png"
          cp "$W/placement_review/nonconverged_preview_top.png" \
            "$W/placement_review/placement_preview_top.png"
          echo "  ⚠️ connectivity-only: promoted diagnostic placeholder scene"
        fi
        if [ -s "$W/work/scene.blend" ] && [ -s "$W/manifest.json" ]; then
          MACHINE_REVIEW_ACCEPTED=0
          echo "  ⚠️ connectivity-only: machine placement quality FAIL retained"
          break
        fi
      fi
      die "machine placement contract did not converge or produced no usable scene"
    fi
    machine_round=$((machine_round + 1))
    revision_dir=$(printf '%s/placement_review/composer_revisions/round_%02d' \
      "$W" "$machine_round")
    mkdir -p "$revision_dir"
    cp "$W/3_composer/edit_plan.json" "$revision_dir/edit_plan_before.json"
    cp "$W/placement_review/placement_review.json" "$revision_dir/machine_review.json"
    failure_stage=$("$PY" -c \
      'import json,sys; print(json.load(open(sys.argv[1])).get("failure_stage", ""))' \
      "$W/placement_review/placement_review.json")
    if { [ "$MACHINE_COMPOSER_VISION" = 1 ] || \
         [ "$MACHINE_COMPOSER_REGION_MAP" = 1 ]; } && \
        [ "$failure_stage" = placement_projection ]; then
      # Projection validation returns before the normal previews are rendered.
      # A preview left by an earlier round would therefore be stale.  Render
      # this rejected proposal from the immutable baked scene with a derived
      # diagnostic-only plan, keeping the canonical plan/review untouched.
      "$PY" stage_exec/prepare_nonconverged_preview.py \
        "$W/3_composer/edit_plan.json" "$revision_dir/diagnostic_edit_plan.json" \
        || die "prepare round $machine_round projection diagnostic plan failed"
      CUDA_VISIBLE_DEVICES=${GPUS%%,*} PYTHONPATH="$ROOT/core:$INF" \
        BI_FRAME_START="$FRAME_START" BI_FRAME_END="$FRAME_END" \
        "$PY" stage_exec/run_initial_placement_batch.py -- \
          "$BAKED" "$revision_dir/diagnostic_scene.blend" \
          "$revision_dir/diagnostic_edit_plan.json" \
          "$revision_dir/diagnostic_report.json" \
          "$revision_dir/diagnostic_review.json" \
          "$revision_dir/placement_preview.png" \
          "$revision_dir/placement_preview_top.png" "$FRAME_START" \
          > "$revision_dir/diagnostic_render.log" 2>&1 \
        || { tail -30 "$revision_dir/diagnostic_render.log"; \
             die "round $machine_round projection diagnostic render failed"; }
      [ -s "$revision_dir/placement_preview.png" ] \
        || die "round $machine_round projection diagnostic front preview missing"
      [ -s "$revision_dir/placement_preview_top.png" ] \
        || die "round $machine_round projection diagnostic top preview missing"
      echo "  diagnostic front/top attached for projection failure"
    else
      [ ! -s "$W/placement_review/placement_preview.png" ] || \
        cp "$W/placement_review/placement_preview.png" "$revision_dir/placement_preview.png"
      [ ! -s "$W/placement_review/placement_preview_top.png" ] || \
        cp "$W/placement_review/placement_preview_top.png" "$revision_dir/placement_preview_top.png"
    fi
    echo "  ↻ machine review failed; placement revision $machine_round/$MACHINE_COMPOSER_REVIEW_ROUNDS"
    REVISION_CONTEXT_ARGS=()
    if [ "$MACHINE_COMPOSER_VISION" = 1 ]; then
      REVISION_CONTEXT_ARGS+=(
        --history-root "$W/placement_review/composer_revisions")
      [ ! -s "$revision_dir/placement_preview.png" ] || \
        REVISION_CONTEXT_ARGS+=(--preview "$revision_dir/placement_preview.png")
      [ ! -s "$revision_dir/placement_preview_top.png" ] || \
        REVISION_CONTEXT_ARGS+=(--top-preview "$revision_dir/placement_preview_top.png")
    fi
    REGION_MAP_ELIGIBLE=0
    if [ "$MACHINE_COMPOSER_REGION_MAP" = 1 ] && \
       [ "$MAP_FIRST_ACTIVE" = 1 ]; then
      if PYTHONPATH="$ROOT/core:$ROOT:$INF" "$PY" -c \
        'import json,sys; from placement_region_contract import map_managed_failed_instances; result=map_managed_failed_instances(json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))); raise SystemExit(0 if result else 1)' \
        "$W/3_composer/edit_plan.json" \
        "$W/placement_review/placement_review.json" \
        > "$revision_dir/region_mode_eligibility.log" 2>&1; then
        REGION_MAP_ELIGIBLE=1
      else
        echo "  REGION_MAP_MODE_UNSUPPORTED: non-map-managed failure uses structured Composer revision"
      fi
    fi
    if [ "$REGION_MAP_ELIGIBLE" = 1 ]; then
      echo "  MAP_FIRST_DETERMINISTIC_RETRY: arrangement-aware fresh green cells"
      echo "  NO_PLACEMENT_AGENT_CALL"
      echo "  NO_CANDIDATE_SELECTOR_LOOP"
      echo "  REUSE_INITIAL_REGION_MAP: only the tried-cell mask changes"
      REGION_MAP_INDEX="$INITIAL_MAP_DIR/region_map_index.json"
      mkdir -p "$revision_dir/selected_maps"
      PYTHONPATH="$ROOT/core:$ROOT:$INF" "$PY" \
        stage_exec/apply_placement_region_selection.py \
        --plan "$W/3_composer/edit_plan.json" \
        --review "$W/placement_review/placement_review.json" \
        --map-index "$REGION_MAP_INDEX" \
        --auto-select \
        --selection-output "$revision_dir/region_selection.json" \
        --history-root "$W/placement_review" \
        --output "$revision_dir/edit_plan_after.json" \
        --application "$revision_dir/region_application.json" \
        --infeasibility-output "$revision_dir/region_selection_infeasible.json" \
        > "$revision_dir/region_application.log" 2>&1 \
        || { tail -30 "$revision_dir/region_application.log"; \
             die "no fresh machine-feasible region cell remains"; }
      PYTHONPATH="$ROOT/core:$ROOT:$ROOT/stage_exec:$INF" "$PY" \
        stage_exec/annotate_placement_region_map.py \
        --map-index "$REGION_MAP_INDEX" \
        --application "$revision_dir/region_application.json" \
        --history-root "$W/placement_review" \
        --output-dir "$revision_dir/selected_maps" \
        --label "deterministic retry round $machine_round" \
        > "$revision_dir/region_annotation.log" 2>&1 \
        || { tail -30 "$revision_dir/region_annotation.log"; \
             die "round $machine_round region-map annotation failed"; }
      LAST_MAP_INDEX="$REGION_MAP_INDEX"
      LAST_APPLICATION="$revision_dir/region_application.json"
      LAST_SELECTION="$revision_dir/region_selection.json"
      LAST_ATTEMPT_DIR="$revision_dir"
    else
      if [ "$COMPOSER_IMAGE_PLACEMENT" = 1 ]; then
        echo "  COMPOSER_IMAGE_RETRY: Composer revises screen positions from Machine Review"
      else
        echo "  PLACEMENT_AGENT_CALL: structured retry is limited to unsupported non-conform failures"
      fi
      PYTHONPATH="$ROOT/core:$ROOT:$INF" "$PY" planner/composer_machine_reviser.py \
        --plan "$W/3_composer/edit_plan.json" \
        --review "$W/placement_review/placement_review.json" \
        "${REVISION_CONTEXT_ARGS[@]}" \
        --output "$revision_dir/edit_plan_after.json" \
        > "$revision_dir/composer_revision.log" 2>&1 \
        || { tail -30 "$revision_dir/composer_revision.log"; die "Composer machine revision failed"; }
      if [ "$COMPOSER_IMAGE_PLACEMENT" = 1 ] && [ -n "$IMAGE_REGION_MAP_INDEX" ]; then
        PYTHONPATH="$ROOT/core:$ROOT:$ROOT/stage_exec:$INF" "$PY" \
          planner/composer_image_placement_selector.py \
          --plan "$W/3_composer/edit_plan.json" \
          --scene-render "$INPUT_DIR/render.png" \
          --manifest "$W/scene_manifest.json" \
          --map-index "$IMAGE_REGION_MAP_INDEX" \
          --output "$revision_dir/composer_image_resnap_selection.json" \
          --application "$revision_dir/composer_image_resnap_application.json" \
          --plan-output "$revision_dir/edit_plan_after_resnap.json" \
          --from-plan-screen-positions \
          --apply \
          > "$revision_dir/composer_image_resnap.log" 2>&1 \
          || { tail -30 "$revision_dir/composer_image_resnap.log"; \
               die "Composer image placement resnap failed"; }
      fi
    fi
    REBUILD_REPORT_OUT="$W/report.json"
    REBUILD_REVIEW_OUT="$W/placement_review/placement_review.json"
    REBUILD_PREVIEW_OUT="$W/placement_review/placement_preview.png"
    REBUILD_TOP_OUT="$TOP_PREVIEW_OUT"
    if [ "$REGION_MAP_ELIGIBLE" = 1 ]; then
      REBUILD_REPORT_OUT="$revision_dir/selected_report.json"
      REBUILD_REVIEW_OUT="$revision_dir/selected_machine_review.json"
      REBUILD_PREVIEW_OUT="$revision_dir/selected_placement_preview.png"
      REBUILD_TOP_OUT="$revision_dir/selected_placement_preview_top.png"
    fi
    CUDA_VISIBLE_DEVICES=${GPUS%%,*} PYTHONPATH="$ROOT/core:$INF" \
      BI_FRAME_START="$FRAME_START" BI_FRAME_END="$FRAME_END" \
      "$PY" stage_exec/run_initial_placement_batch.py -- \
        "$BAKED" "$W/work/scene.blend" \
        "$W/3_composer/edit_plan.json" "$REBUILD_REPORT_OUT" \
        "$REBUILD_REVIEW_OUT" "$REBUILD_PREVIEW_OUT" \
        "$REBUILD_TOP_OUT" "$FRAME_START" "${REGION_REVIEW_ARGS[@]}" \
        "${SEMANTIC_GRID_ARGS[@]}" \
        > "$revision_dir/rebuild.log" 2>&1 \
      || { tail -30 "$revision_dir/rebuild.log"; die "Composer revision rebuild failed"; }
    if [ "$REGION_MAP_ELIGIBLE" = 1 ] && [ ! -s "$REBUILD_PREVIEW_OUT" ]; then
      "$PY" stage_exec/prepare_nonconverged_preview.py \
        "$W/3_composer/edit_plan.json" \
        "$revision_dir/selected_diagnostic_edit_plan.json" \
        || die "prepare round $machine_round selected diagnostic plan failed"
      CUDA_VISIBLE_DEVICES=${GPUS%%,*} PYTHONPATH="$ROOT/core:$INF" \
        BI_FRAME_START="$FRAME_START" BI_FRAME_END="$FRAME_END" \
        "$PY" stage_exec/run_initial_placement_batch.py -- \
          "$BAKED" "$revision_dir/selected_diagnostic_scene.blend" \
          "$revision_dir/selected_diagnostic_edit_plan.json" \
          "$revision_dir/selected_diagnostic_report.json" \
          "$revision_dir/selected_diagnostic_review.json" \
          "$REBUILD_PREVIEW_OUT" "$REBUILD_TOP_OUT" "$FRAME_START" \
          > "$revision_dir/selected_diagnostic_render.log" 2>&1 \
        || { tail -30 "$revision_dir/selected_diagnostic_render.log"; \
             die "round $machine_round selected diagnostic render failed"; }
    fi
    if [ "$REGION_MAP_ELIGIBLE" = 1 ]; then
      cp "$REBUILD_REVIEW_OUT" "$W/placement_review/placement_review.json"
      [ ! -s "$REBUILD_REPORT_OUT" ] || cp "$REBUILD_REPORT_OUT" "$W/report.json"
      [ ! -s "$revision_dir/selected_manifest.json" ] || \
        cp "$revision_dir/selected_manifest.json" "$W/manifest.json"
      [ ! -s "$REBUILD_PREVIEW_OUT" ] || \
        cp "$REBUILD_PREVIEW_OUT" "$W/placement_review/placement_preview.png"
      [ ! -s "$REBUILD_TOP_OUT" ] || \
        cp "$REBUILD_TOP_OUT" "$W/placement_review/placement_preview_top.png"
    else
      cp "$W/placement_review/placement_review.json" \
        "$revision_dir/selected_machine_review.json"
      [ ! -s "$W/placement_review/placement_preview.png" ] || \
        cp "$W/placement_review/placement_preview.png" \
          "$revision_dir/selected_placement_preview.png"
      [ ! -s "$W/placement_review/placement_preview_top.png" ] || \
        cp "$W/placement_review/placement_preview_top.png" \
          "$revision_dir/selected_placement_preview_top.png"
    fi
  done
  if [ "$MACHINE_REVIEW_ACCEPTED" = 1 ]; then
    echo "  ✅ machine placement review PASS after $machine_round placement revision(s)"
  else
    echo "  ⚠️ machine placement review remains unqualified; continuing for connectivity"
  fi
  if [ "$MAP_FIRST_ACTIVE" = 1 ]; then
    FINAL_PLACEMENT_DIR="$W/placement_review/final"
    mkdir -p "$FINAL_PLACEMENT_DIR/selected_maps"
    PYTHONPATH="$ROOT/core:$ROOT:$ROOT/stage_exec:$INF" "$PY" \
      stage_exec/annotate_placement_region_map.py \
      --map-index "$INITIAL_MAP_DIR/region_map_index.json" \
      --application "$LAST_APPLICATION" \
      --history-root "$W/placement_review" \
      --output-dir "$FINAL_PLACEMENT_DIR/selected_maps" \
      --label "final deterministic selection" \
      > "$FINAL_PLACEMENT_DIR/region_annotation.log" 2>&1 \
      || { tail -30 "$FINAL_PLACEMENT_DIR/region_annotation.log"; \
           die "final region-map annotation failed"; }
    cp "$LAST_APPLICATION" "$FINAL_PLACEMENT_DIR/region_application.json"
    cp "$LAST_SELECTION" "$FINAL_PLACEMENT_DIR/region_selection.json"
    cp "$W/3_composer/edit_plan.json" "$FINAL_PLACEMENT_DIR/edit_plan.json"
    cp "$W/placement_review/placement_review.json" \
      "$FINAL_PLACEMENT_DIR/machine_review.json"
    cp "$W/placement_review/placement_preview.png" \
      "$FINAL_PLACEMENT_DIR/placement_preview.png"
    cp "$W/placement_review/placement_preview_top.png" \
      "$FINAL_PLACEMENT_DIR/placement_preview_top.png"
    echo "  final map/cells/views → $FINAL_PLACEMENT_DIR"
  fi
  if [ "$PLACEMENT_ONLY_E2E" = 1 ]; then
    echo "  ⏭️  placement-only：placeholder 不需要 generated code，跳過 fillcode"
  else
    "$PY" pipeline/glue.py fillcode --manifest "$W/manifest.json" \
        --table "$W/overlay_table.json" || die "code 回填失敗"
  fi
  # Composer authors the JSON; Step 8 is the first stage that can render its
  # actual placeholder result. Keep the final executed views beside Composer's
  # files so a human can review plan + outcome in one directory.
  [ ! -s "$W/placement_review/placement_preview.png" ] || \
    cp "$W/placement_review/placement_preview.png" \
      "$W/3_composer/final_placeholder_placement.png"
  [ ! -s "$W/placement_review/placement_preview_top.png" ] || \
    cp "$W/placement_review/placement_preview_top.png" \
      "$W/3_composer/final_placeholder_placement_top.png"
  "$PY" -c \
    'import json,sys; from pathlib import Path; out=Path(sys.argv[1]); data={"role":"executed Step-8 placeholder result for the Composer plan","plan":str(Path(sys.argv[2]).resolve()),"review":str(Path(sys.argv[3]).resolve()),"front":str(Path(sys.argv[4]).resolve()) if Path(sys.argv[4]).is_file() else None,"top":str(Path(sys.argv[5]).resolve()) if Path(sys.argv[5]).is_file() else None}; out.write_text(json.dumps(data,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")' \
    "$W/3_composer/final_placeholder_placement.json" \
    "$W/3_composer/edit_plan.json" \
    "$W/placement_review/placement_review.json" \
    "$W/3_composer/final_placeholder_placement.png" \
    "$W/3_composer/final_placeholder_placement_top.png" \
    || die "Composer final placeholder provenance write failed"
  echo "  placement preview → $W/placement_review/placement_preview.png"
  if [ "$INITIAL_PLACEMENT_TOP_PREVIEW" = 1 ] || [ "$MAP_FIRST_ACTIVE" = 1 ]; then
    echo "  top preview       → $W/placement_review/placement_preview_top.png"
  else
    echo "  ⏭️  one-shot：跳過 top preview"
  fi
  echo "  review contract   → $W/placement_review/placement_review.json"
fi

if [ "$PLACEMENT_ONLY_E2E" = 1 ] || [ "$STOP_AFTER_PLACEMENT_REVIEW" = 1 ]; then
  [ -s "$W/placement_review/placement_preview.png" ] \
    || die "STOP_AFTER_PLACEMENT_REVIEW=1 但 preview 不存在"
  echo
  echo "★★ E2E_PLACEMENT_REVIEW_READY  $CASE → $W/placement_review ★★"
  echo "   map-first surface/volume failure 會由 deterministic fresh-cell retry 處理"
  [ "$PLACEMENT_ONLY_E2E" != 1 ] || echo "   mode=placement-only（未執行 asset generator / overlay / populate / fine / render）"
  exit 0
fi

# ── 9 ★階段 C★ placer：輸出只能叫 populated，不能冒充 fine ───────────────
if [ "$FROM" -le 9 ]; then
  step 9 "placer(階段 C) → populated/scene.blend"
  invalidate_render_gate
  mkdir -p "$W/populated"
  T0=$(date +%s)
  # 一次 populate：replacement 是整個 factory 類別，無須隔離兩種 TreeFactory。
  CUDA_VISIBLE_DEVICES=${GPUS%%,*} PYTHONPATH="$OV:$ROOT/core:$INF" \
    BI_FRAME_START="$FRAME_START" BI_FRAME_END="$FRAME_END" \
    BI_ALLOW_UNQUALIFIED_OUTPUT="$CONNECTIVITY_SMOKE" \
    "$PY" stage_exec/placer.py -- \
      "$W/work/scene.blend" "$W/populated/scene.blend" "$W/manifest.json" \
      "$W/placer_report.json" > "$L"_placer.log 2>&1
  rc=$?; echo "  placer rc=$rc ★$(( $(date +%s)-T0 )) 秒★"
  grep -aE "^\[placer\]|^\[pool\]" "$L"_placer.log | tail -16
  [ $rc -ne 0 ] && { tail -25 "$L"_placer.log; die "placer 失敗"; }
  [ -s "$W/populated/scene.blend" ] || die "placer 沒有輸出 populated/scene.blend"
  link_stage_metadata "$W/work" "$W/populated"
  PLACER_CONTRACT_RC=0
  "$PY" stage_exec/check_placer_contract.py \
      --placer-report "$W/placer_report.json" --manifest "$W/manifest.json" \
      --input-blend "$W/work/scene.blend" --output-blend "$W/populated/scene.blend" \
      --frame-start "$FRAME_START" --frame-end "$FRAME_END" \
    || PLACER_CONTRACT_RC=$?
  if [ "$PLACER_CONTRACT_RC" -ne 0 ]; then
    if [ "$CONNECTIVITY_SMOKE" = 1 ]; then
      echo "  ⚠️ connectivity-only: placer quality FAIL retained; continuing"
    elif { [ "${BI_ABLATION_NO_PLACEMENT_GATE:-0}" = 1 ] || [ "$BI_SOFT_GATES" = 1 ]; }; then
      # ★ablation：pipeline 層的第二道 gate 也要放行★(2026-09-21)
      #   placer 內部的 raise 已由同一個旗標繞過，但 run_e2e 還會再用
      #   check_placer_contract.py 驗一次 report ⇒ 不一起放行就還是出不了圖。
      #   不合格的事實仍完整留在 placer_report.json 供離線量測。
      echo "  ★ablation：placer contract FAIL 不擋，繼續 fine_terrain★"
    else
      die "placer contract FAIL（不浪費時間跑 fine_terrain）"
    fi
  fi
  echo "  populated → $W/populated/scene.blend（尚未取得 render 資格）"
fi

if [ "$STOP_AFTER_POPULATE" = 1 ]; then
  [ -s "$W/populated/scene.blend" ] || die "STOP_AFTER_POPULATE=1 但 populated scene 不存在"
  echo
  echo "★★ E2E_POPULATE_DONE  $CASE → $W ★★"
  exit 0
fi

# ── 10 ★真正的 fine_terrain★ ─────────────────────────────────────────────
if [ "$FROM" -le 10 ]; then
  step 10 "genuine fine_terrain → fine/scene.blend"
  [ "$SCENE_LOCATION" = "outdoor" ] || \
    die "目前 full E2E 的 Step 10 只支援 outdoor generate_nature fine_terrain；indoor baseline 已選好，但 indoor finalization 尚未接上"
  invalidate_render_gate
  [ -s "$W/populated/scene.blend" ] || die "缺 populated/scene.blend，先跑 step 9"
  [ -f "$W/populated/MaskTag.json" ] || die "populated 缺 MaskTag.json"
  [ -f "$W/populated/assets/info.pickle" ] || die "populated 缺 assets/info.pickle"
  PLACER_CONTRACT_RC=0
  "$PY" stage_exec/check_placer_contract.py \
      --placer-report "$W/placer_report.json" --manifest "$W/manifest.json" \
      --input-blend "$W/work/scene.blend" --output-blend "$W/populated/scene.blend" \
      --frame-start "$FRAME_START" --frame-end "$FRAME_END" \
    || PLACER_CONTRACT_RC=$?
  if [ "$PLACER_CONTRACT_RC" -ne 0 ]; then
    if [ "$CONNECTIVITY_SMOKE" = 1 ]; then
      echo "  ⚠️ connectivity-only: pre-fine placer quality FAIL retained"
    elif { [ "${BI_ABLATION_NO_PLACEMENT_GATE:-0}" = 1 ] || [ "$BI_SOFT_GATES" = 1 ]; }; then
      echo "  ★ablation：pre-fine placer contract FAIL 不擋★"
    else
      die "placer contract FAIL（禁止進入 fine_terrain）"
    fi
  fi
  mkdir -p "$W/contracts" "$W/fine"

  # fine 前先把不變量存證；它也提供實際 camera FOV 給 auto mesher routing。
  CUDA_VISIBLE_DEVICES=${GPUS%%,*} PYTHONPATH="$OV:$ROOT/core:$INF" "$PY" \
      stage_exec/snapshot_scene.py -- "$W/populated/scene.blend" \
      "$W/contracts/populated.json" --frames "$CONTRACT_FRAMES" \
      > "$L"_snapshot_populated.log 2>&1 \
    || { tail -25 "$L"_snapshot_populated.log; die "populated snapshot 失敗"; }
  "$PY" -c \
    "import json,sys; d=json.load(open(sys.argv[1])); assert d['camera']['active'], 'populated scene 沒有 active camera'" \
    "$W/contracts/populated.json" || die "fine_terrain 前置 camera contract 失敗"

  MESHER=$FINE_MESHER
  if [ "$MESHER" = auto ]; then
    MESHER=OcMesher
    echo "  FINE_MESHER=auto ⇒ 保守使用 OcMesher（expanded multi-camera FOV 無法由單格 FOV 判斷）"
  fi
  case "$MESHER" in
    OcMesher|SphericalMesher) ;;
    *) die "FINE_MESHER 必須是 auto / OcMesher / SphericalMesher（收到 $MESHER）" ;;
  esac
  echo "$MESHER" > "$W/contracts/mesher.txt"
  echo "  mesher=$MESHER  terrain_asset_instances=$TERRAIN_ASSET_INSTANCES  frames=$FRAME_START..$FRAME_END"

  T0=$(date +%s)
  ( cd "$INF" && \
    CUDA_VISIBLE_DEVICES=${GPUS%%,*} PYTHONPATH="$OV:$ROOT/core:$INF" "$PY" \
      -m infinigen_examples.generate_nature --seed "$SEED" --task fine_terrain \
      --input_folder "$W/populated" --output_folder "$W/fine" -g "$SCENE" \
      -p execute_tasks.frame_range=[$FRAME_START,$FRAME_END] \
         fine_terrain.mesher_backend=$MESHER \
         infinigen.terrain.elements.landtiles.load_assets.on_the_fly_instances=$TERRAIN_ASSET_INSTANCES \
         infinigen.terrain.elements.caves.load_assets.on_the_fly_instances=$TERRAIN_ASSET_INSTANCES \
         infinigen.terrain.elements.upsidedown_mountains.load_assets.on_the_fly_instances=$TERRAIN_ASSET_INSTANCES \
  ) > "$L"_fine_terrain.log 2>&1
  rc=$?; echo "  fine_terrain rc=$rc ★$(( $(date +%s)-T0 )) 秒★"
  [ $rc -ne 0 ] && { tail -35 "$L"_fine_terrain.log; die "fine_terrain 失敗（禁止 fallback 到 populated）"; }
  [ -s "$W/fine/scene.blend" ] || die "fine_terrain 沒有輸出 fine/scene.blend"
fi

# ── 11 final-scene contract：所有 invariant 通過才發 gate ────────────────
if [ "$FROM" -le 11 ]; then
  step 11 "validate final-scene contract"
  invalidate_render_gate
  [ -s "$W/populated/scene.blend" ] || die "缺 populated scene"
  [ -s "$W/fine/scene.blend" ] || die "缺 genuine fine scene"
  [ -f "$W/fine/MaskTag.json" ] || die "fine stage 缺 MaskTag.json"
  [ -f "$W/fine/assets/info.pickle" ] || die "fine stage 缺 assets/info.pickle"
  mkdir -p "$W/contracts"
  CUDA_VISIBLE_DEVICES=${GPUS%%,*} PYTHONPATH="$OV:$ROOT/core:$INF" "$PY" \
      stage_exec/snapshot_scene.py -- "$W/populated/scene.blend" \
      "$W/contracts/populated.json" --frames "$CONTRACT_FRAMES" \
      > "$L"_snapshot_populated.log 2>&1 \
    || { tail -25 "$L"_snapshot_populated.log; die "populated snapshot 失敗"; }
  CUDA_VISIBLE_DEVICES=${GPUS%%,*} PYTHONPATH="$OV:$ROOT/core:$INF" "$PY" \
      stage_exec/snapshot_scene.py -- "$W/fine/scene.blend" \
      "$W/contracts/fine.json" --frames "$CONTRACT_FRAMES" \
      > "$L"_snapshot_fine.log 2>&1 \
    || { tail -25 "$L"_snapshot_fine.log; die "fine snapshot 失敗"; }

  MESHER=unknown
  [ ! -f "$W/contracts/mesher.txt" ] || MESHER=$(cat "$W/contracts/mesher.txt")
  CONTRACT_RC=0
  "$PY" stage_exec/validate_final_scene.py \
      --populated "$W/contracts/populated.json" \
      --fine "$W/contracts/fine.json" \
      --placer-report "$W/placer_report.json" --manifest "$W/manifest.json" \
      --out "$W/final_contract.json" --ok-marker "$W/final_contract.ok" \
      --mesher "$MESHER" > "$L"_final_contract.log 2>&1 \
    || CONTRACT_RC=$?
  cat "$L"_final_contract.log
  if [ "$CONTRACT_RC" -ne 0 ]; then
    if [ "$CONNECTIVITY_SMOKE" = 1 ]; then
      echo "  ⚠️ connectivity-only: contract FAIL retained; diagnostic render may continue"
      echo "  ⚠️ no final_contract.ok will be created and this result is not quality-qualified"
    elif { [ "${BI_ABLATION_NO_PLACEMENT_GATE:-0}" = 1 ] || [ "$BI_SOFT_GATES" = 1 ]; }; then
      # ★ablation：第三道 gate 也放行★ —— 這個條件的前提就是「原樣採用、不檢查」。
      #   ★不建立 final_contract.ok★：這張圖在正式品質標準下仍然是不合格的，
      #   論文引用時必須註明是關掉 gate 後強制渲出來的。
      echo "  ★ablation：final-scene contract FAIL 不擋，仍然 render★"
      echo "     ⚠️ 不會建立 final_contract.ok —— 這個結果不具備品質資格"
    else
      die "final-scene contract FAIL（禁止 render）"
    fi
  fi
fi

# ── 12 render：marker + blend stat 都必須仍與 PASS contract 完全一致 ─────
if [ "$FROM" -le 12 ]; then
  if { [ "${BI_ABLATION_NO_PLACEMENT_GATE:-0}" = 1 ] || [ "$BI_SOFT_GATES" = 1 ]; } && [ ! -f "$W/final_contract.ok" ]; then
    # ★ablation：渲到 view.png（與其他條件同名，方便並排）但不通過 gate★
    step 12 "render unqualified ablation result"
    RENDER_OUTPUT="$W/render/view.png"
    echo "  ★ablation：略過 render gate；final_contract.json 仍是 FAIL★"
  elif [ "$CONNECTIVITY_SMOKE" = 1 ] && [ ! -f "$W/final_contract.ok" ]; then
    step 12 "render unqualified connectivity diagnostic"
    RENDER_OUTPUT="$W/render/connectivity_view.png"
    echo "  ⚠️ bypassing only the render-quality gate; final_contract.json remains FAIL"
  else
    step 12 "render exact validated fine scene"
    "$PY" stage_exec/assert_render_gate.py \
        --contract "$W/final_contract.json" --ok-marker "$W/final_contract.ok" \
        --blend "$W/fine/scene.blend" --frame-start "$FRAME_START" \
        --frame-end "$FRAME_END" || die "render gate 拒絕"
    RENDER_OUTPUT="$W/render/view.png"
  fi
  mkdir -p "$W/render"
  # ★共用卡 OOM 就等再試★(2026-10-20)：1019 重跑 12/15 都死於 "System is out of GPU memory"
  RENDER_OOM_RETRIES=${RENDER_OOM_RETRIES:-3}; RENDER_OOM_WAIT=${RENDER_OOM_WAIT:-600}
  for _try in $(seq 0 "$RENDER_OOM_RETRIES"); do
    CUDA_VISIBLE_DEVICES=${GPUS%%,*} PYTHONPATH="$OV:$ROOT/core:$INF" "$PY" stage_exec/render_view.py -- \
        "$W/fine/scene.blend" "$RENDER_OUTPUT" --as-is \
        --frame "$FRAME_START" --res "$STILL_RENDER_WIDTH" "$STILL_RENDER_HEIGHT" \
        --samples "$STILL_RENDER_SAMPLES" --view AgX \
        > "$L"_render.log 2>&1
    rc=$?
    if [ $rc -ne 0 ] && grep -aq "out of GPU memory" "$L"_render.log && [ "$_try" -lt "$RENDER_OOM_RETRIES" ]; then
      echo "  ⚠️ render GPU OOM (GPU ${GPUS%%,*} 被佔用)，等 ${RENDER_OOM_WAIT}s 再試 ($((_try+1))/$RENDER_OOM_RETRIES)"; sleep "$RENDER_OOM_WAIT"; continue
    fi
    break
  done
  echo "  render rc=$rc  → $RENDER_OUTPUT"
  if grep -aq "BI_RENDER_DEVICE_FALLBACK_CPU" "$L"_render.log 2>/dev/null; then
    echo "  ⛔⛔ ★這次 render 跑在 CPU 上★(GPUS=$GPUS 找不到可用的卡)"
    echo "     時間數字不可與 GPU 的歷史紀錄比較；請確認 nvidia-smi -L 與卡是否被佔用"
  fi
  [ $rc -ne 0 ] && { tail -25 "$L"_render.log; die "render 失敗"; }

  if [ "$RENDER_VIDEO" = 1 ]; then
    for _try in $(seq 0 "${RENDER_OOM_RETRIES:-3}"); do
      CUDA_VISIBLE_DEVICES=${GPUS%%,*} PYTHONPATH="$OV:$ROOT/core:$INF" "$PY" \
          stage_exec/render_animation.py -- "$W/fine/scene.blend" "$W/render/frames" \
          --frame-start "$FRAME_START" --frame-end "$FRAME_END" \
          --fps "$VIDEO_FPS" --samples "$VIDEO_SAMPLES" --res "$VIDEO_RES" --view AgX \
          --camera-move "${VIDEO_CAMERA_MOVE:-auto}" --camera-move-m "${VIDEO_CAMERA_MOVE_M:-0.8}" --scene "$SCENE" \
          > "$L"_render_animation.log 2>&1
      rc=$?
      if [ $rc -ne 0 ] && grep -aq "out of GPU memory" "$L"_render_animation.log && [ "$_try" -lt "${RENDER_OOM_RETRIES:-3}" ]; then
        echo "  ⚠️ animation render GPU OOM，等 ${RENDER_OOM_WAIT:-600}s 再試 ($((_try+1))/${RENDER_OOM_RETRIES:-3})"; sleep "${RENDER_OOM_WAIT:-600}"; continue
      fi
      break
    done
    [ $rc -eq 0 ] || { tail -30 "$L"_render_animation.log; die "animation render 失敗"; }
    if command -v ffmpeg >/dev/null; then
      N_FRAMES=$(( FRAME_END - FRAME_START + 1 ))
      ffmpeg -y -v error -framerate "$VIDEO_FPS" -start_number "$FRAME_START" \
        -i "$W/render/frames/frame_%04d.png" -frames:v "$N_FRAMES" \
        -c:v libx264 -pix_fmt yuv420p -crf 18 "$W/render/final.mp4" \
        || die "ffmpeg mp4 失敗"
      echo "  video → $W/render/final.mp4"
    else
      echo "  ⚠️ ffmpeg 不存在；PNG animation frames 已完成"
    fi
  fi
fi

if [ "$CONNECTIVITY_SMOKE" = 1 ] && [ ! -f "$W/final_contract.ok" ]; then
  echo; echo "★★ E2E_CONNECTIVITY_DONE_UNQUALIFIED  $CASE → $W ★★"
else
  echo; echo "★★ E2E_DONE  $CASE → $W ★★"
fi
