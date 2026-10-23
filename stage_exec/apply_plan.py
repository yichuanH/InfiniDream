"""
apply_plan —— ★單一入口★：一份 JSON 同時描述 HERO 與 EXTRA 的編輯，內部分派執行。

★設計原則(使用者訂的)★
  ★JSON 只描述「操作」，不描述「改成什麼樣」。★
  外觀細節一律來自 ★asset generator 產出的 Factory code★(overlay 目錄 + factory.txt)，
  執行層只讀 code，★不需要知道裡面改了什麼★。

────────────────────────────────────────────────────────────────
★兩類物件的能力差異(這是機制決定的，不是還沒實作)★
────────────────────────────────────────────────────────────────
  ┌──────────┬──────────────────────┬────────────────────────────────┐
  │ 操作     │ HERO(placeholder)    │ EXTRA(ground_scatter)          │
  ├──────────┼──────────────────────┼────────────────────────────────┤
  │ replace  │ ✅ 換 factory code   │ ✅ 換母本集合                  │
  │ delete   │ ✅ 刪 placeholder    │ ✅ density=0                   │
  │ density  │ ❌(用 add/delete)    │ ✅ 改 float                    │
  │ scale    │ ✅ ★個體★           │ ⚠️ ★只能整批★(改母本 scale)   │
  │ move     │ ✅ ★個體★           │ ❌ ★做不到★                   │
  │ add      │ ✅ ★指定位置★       │ ⚠️ ★只能調密度/新建整個系統★  │
  └──────────┴──────────────────────┴────────────────────────────────┘
  ★EXTRA 的 move / 定點 add 做不到的原因★：撒的位置是 ★GN 在 render 時算的★，
    blend 裡★根本沒有個體★可以指。⇒ validate() 會回 ⛔ 並說明，★不會靜默忽略★。

★「原本沒有的 EXTRA 想加進去」怎麼辦(使用者問的)★
  ⇒ 用 ★op="create"★。實測 generate_nature.py:629-638 建蘑菇就是一行：
        ground_mushroom.Mushrooms().apply(terrain_near, density=..., selection=...)
    這支照抄 —— ★可以在現成 coarse blend 上憑空長出一整個 scatter 系統★，
    不用重跑 coarse。
  ⚠️ ★但「加蘑菇」有兩條路，要看 prompt 要什麼★(0727 3-B 的 mushroom 歧義)：
      地上一片   → ★EXTRA create★(本支)，便宜、不用 populate、位置由 GN 撒
      指定幾朵/可個別操作 → ★HERO scatter★(scatter_new_hero.py)，
                            要 populate，但之後可以移動/縮放單一朵
    ⇒ ★預設建議 EXTRA create★(便宜)，除非 prompt 明確要「某一朵」。

────────────────────────────────────────────────────────────────
JSON 格式
────────────────────────────────────────────────────────────────
{
  "hero_edits": [
    {"op":"delete","factory":"TreeFactory","rule":"frac:0.5"},
    {"op":"scale", "factory":"TreeFactory","value":0.5},
    {"op":"move",  "factory":"TreeFactory","delta":[0,0,8]},
    {"op":"add",   "factory":"BushFactory","n":60,"radius":10}
  ],
  "extra_edits": [
    {"op":"replace","target":"PineNeedleFactory","code":"generated/gold_needle"},
    {"op":"delete", "target":"PineconeFactory"},
    {"op":"density","target":"FernFactory","value":3.0},
    {"op":"scale",  "target":"FernFactory","value":2.0},
    {"op":"create", "target":"mushroom","density":2.0}
  ]
}

用法:
  python apply_plan.py -- <in.blend> <out.blend> <plan.json> <report.json>
  python apply_plan.py -- <in.blend> --dry-run <plan.json> <report.json>

⚠️ hero_edits 若有 add/delete/move/scale ⇒ ★之後必須重跑 populate★(~10分鐘)
   extra_edits ⇒ ★不用 populate★，改完直接 render(~30秒)
   report 的 needs_populate 欄位會標出來。
"""
import bpy, sys, json, importlib.util, re, math, random, os, hashlib
from copy import deepcopy
from pathlib import Path
from mathutils import Euler, Vector

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import surface_registry as sr
import scatter_catalog as sc
import official_scatter as osc
import scatter_adapters as scatter_adapters
import scatter_visibility as scatter_visibility
from overlay_imports import (
    reset_gin_state,
    load_overlay_factory as _shared_load_overlay_factory,
)
from placement_region_contract import (
    classify_placeholder_population_intent,
    placeholder_collection_claimed_by_plan,
    requires_full_footprint_structure_support,
    uses_surface_map,
)
import placeholder_population as php

argv = sys.argv[sys.argv.index("--") + 1:]
IN_BLEND, OUT_BLEND, PLAN_JSON, REPORT_JSON = argv[0], argv[1], argv[2], argv[3]
DRY_RUN = (OUT_BLEND == "--dry-run")
random.seed(0)

# ★★出圖用哪台相機，擺放就要用同一台★★(2026-08-09)
#   render_view.py / composer.py 都不是用場景原生的相機出圖，而是把它★抬高/加俯角/
#   後退/橫移★之後才拍。擺放卻一直用原生那台 ⇒ 兩個座標系從來沒對齊過。
#   ⚠️ ★誤差會被距離放大★：
#     ・forest 落點在 10m，偏移造成的偏差還在畫面內 ⇒ 看得到，以為對了
#     ・snowy_mountain 落點在 150m，★光是俯角差 12.3° 就超過垂直半視角 10.2°★
#       ⇒ 4 棵樹/3 隻麋鹿/6 個禮物盒/1 台雪橇全部 populate 成功，畫面上一個都沒有
#   ⇒ 這幾個參數由呼叫者傳進來，與渲染那一步★用同一組數字★(run_e2e.sh 是唯一來源)。
#     預設 0 = 用原生相機(不傳就是舊行為)。
def _argf(flag, d):
    return float(argv[argv.index(flag) + 1]) if flag in argv else d


CAM_VIEW = dict(aerial=_argf("--cam-aerial", 0.0), pitch=_argf("--cam-pitch", 0.0),
                back=_argf("--cam-back", 0.0), side=_argf("--cam-side", 0.0),
                yaw=_argf("--cam-yaw", 0.0))
CAM_VIEW_ON = any(v for v in CAM_VIEW.values())

PH_COLL_RE = re.compile(r"placeholders:((.*)\((\d*)\))")

HERO_FACTORIES = {
    "TreeFactory", "BoulderFactory", "BushFactory", "KelpMonocotFactory",
    "CactusFactory", "CloudFactory", "GlowingRocksFactory", "DragonflyFactory",
    "CachedTreeFactory", "CachedBoulderFactory", "CachedBushFactory",
    "CachedCactusFactory",
}
HERO_OPS = {"delete", "scale", "move", "add"}
# ★不動 box、只帶擺放屬性的一筆★(2026-08-06)
#   ⚠️ ★為什麼需要這個 op★：hero 的 replace 不是階段 A 的動作(box 不動，換的是
#      populate 時用哪份 code)，所以 composer 只把它寫進 appearance.json。
#      但 anchoring / n_masters ★是 replace 才需要的擺放屬性，appearance 沒有程式在讀★，
#      於是「換上去之後怎麼擺」在 director → apply_plan 之間掉了。
#      (2026-08-05 端到端第一次跑 replace 才發現；先前所有測試都是 run 腳本
#       手動把 anchoring 塞進 manifest ⇒ 等於自己先填好答案，永遠測不到。)
#   ⇒ composer 為每個 appearance placement 額外吐一筆 op="attrs"，
#     apply_plan 這裡★不執行任何 box 操作★，只讓下面的 plan_attrs 收走屬性。
HERO_ATTR_OPS = {"attrs"}
EXTRA_OPS = {"replace", "delete", "density", "scale", "create"}
# ★EXTRA 明確做不到的★ —— 要回 ⛔ 並說明，不能靜默忽略
EXTRA_IMPOSSIBLE = {
    "move": "ground_scatter 的位置是 GN 在 render 時算的，blend 裡沒有個體可以移動。"
            "若要能移動單一個體，改用 hero scatter(scatter_new_hero.py)。",
}
# 已知可用 op="create" 憑空建立的 EXTRA 系統(對應 generate_nature.py 的 run_stage)
#   value = (scatter 模組, scatter 類名, ★該系統母本用的 AssetFactory fqn★)
#   第三個欄位是給 ★create + code★ 用的：要知道「原本是哪個 factory 生母本」，
#   才能用 generator 改寫版把它換掉。
#   ⚠️ 2026-09-07：這張表原本★只有 mushroom 一筆★，於是 director 想撒任何別的
#      地被(花海/草/碎石)都只能標 infeasible —— 但 create 的機制本身是通用的。
#      現在整份由 scatter_catalog 導出，26 個 infinigen 原生 scatter 全部可建。
# scatter 距相機多近就不撒(公尺)。相機在密林裡推進 4m 時，5m 淨空剛好
# 讓前景保有花但不會有花長進鏡頭。
DEFAULT_CAMERA_CLEAR_M = 5.0
# ★scatter 的距離上限★(2026-09-08)：基底改成整片 OpaqueTerrain(1.4-166m)之後，
#   不設限會一路撒到 166m 外(實測 33949 株)。60m 之外的實例在畫面上只剩幾個像素，
#   砍掉少 3 倍實例而畫面幾乎不變(10879 株)。這同時是★美術旋鈕★ —— 它決定
#   「這片地被延伸到多遠」，不只是省算力。edit 的 camera_far_m 可覆寫，None 解除。
#   ★2026-09-09 改回 None★：基底改成照官方逐支選之後(terrain_inview 只到 ~43m、
#   terrain_near ~22m)，範圍本來就被官方的 LOD 分塊界定了，再加一層距離上限
#   是多餘的，而且官方沒有這個概念。edit 仍可用 camera_far_m 明確指定
#   (SCATTER_BASE=terrain 整片地形時才真的需要)。
DEFAULT_CAMERA_FAR_M = None

CREATABLE_EXTRA = {
    name: (sc.module_path(name), entry["cls"], entry["factory_symbol"])
    for name, entry in sc.SCATTERS.items()
}

plan = json.loads(Path(PLAN_JSON).read_text(encoding="utf-8"))

# ★★逐資產尺寸釘選★★(2026-09-13 使用者：條紋獏/狐狸太小、百合花太大)
#   實作在 stage_exec/asset_scale_pins.py(第 8 步的 simple_volume_placement 會先套，
#   這裡是冪等的第二道 —— 走舊 region map 路徑時由這裡負責)。hero 乘在 proxy_dimensions_m
#   上(資產會被等比 fit 到 proxy ⇒ 等於直接放大成品)；撒點乘在新母本的頂點上(密度自動重算)。
import asset_scale_pins as _pins

ASSET_SCALES = _pins.pins()


def asset_scale_for(*names) -> float:
    return _pins.factor_for(ASSET_SCALES, *names)


if ASSET_SCALES:
    print(f"[plan] 📏 逐資產尺寸釘選: {ASSET_SCALES}", flush=True)
    _pins.apply_to_hero_edits(plan.get("hero_edits", []),
                              log=lambda m: print(m, flush=True))

# ★★逐資產位移動畫釘選★★(2026-09-13)：實作同樣在 asset_scale_pins(冪等)。
#   第 8 步的 simple_volume_placement 會先套 —— 擺放必須看得到「這隻會走動」，
#   不然不會預留掃過範圍(14:32 coast：10 隻紅鶴照 1.2m 排下去，走起來整批互撞)。
_pins.apply_motions_to_hero_edits(plan.get("hero_edits", []),
                                  log=lambda m: print(m, flush=True))
# ★會飛的不要原地浮★：要在打 bi_world_motion_* stamp(下面 ~1632 行)之前改好，placer 才找得到 anchor
_pins.normalize_flying_motions(plan.get("hero_edits", []),
                               log=lambda m: print(m, flush=True))
# ★撒點密度等級釘選★(2026-09-13 使用者：蘑菇 very dense)
_pins.apply_density_to_edits(plan.get("extra_edits", []), plan.get("official_scatter_edits", []),
                             log=lambda m: print(m, flush=True))
# ★母本池大小釘選★(2026-09-13 使用者：珊瑚母本可以放 10 個嗎)
_pins.apply_masters_to_edits(plan.get("hero_edits", []), plan.get("extra_edits", []),
                             plan.get("official_scatter_edits", []), plan.get("swarm_edits", []),
                             log=lambda m: print(m, flush=True))

hero_edits = plan.get("hero_edits", [])
extra_edits = plan.get("extra_edits", [])
swarm_edits = plan.get("swarm_edits", [])
official_scatter_edits = plan.get("official_scatter_edits", [])
print(f"[plan] hero={len(hero_edits)} extra={len(extra_edits)} "
      f"swarm={len(swarm_edits)} official_scatter={len(official_scatter_edits)}",
      flush=True)

bpy.ops.wm.open_mainfile(filepath=IN_BLEND)
scene = bpy.context.scene
surface_scene_name = sr.canonical_scene_name(
    str(plan.get("scene_type") or "")
)
if not sr.scene_rules(surface_scene_name):
    surface_scene_name = next((
        part for part in reversed(Path(IN_BLEND).parts)
        if sr.scene_rules(part)
    ), "")
frame_start_env = os.environ.get("BI_FRAME_START")
frame_end_env = os.environ.get("BI_FRAME_END")
if (frame_start_env is None) != (frame_end_env is None):
    raise ValueError("BI_FRAME_START and BI_FRAME_END must be provided together")
if frame_start_env is not None:
    frame_start = int(frame_start_env)
    frame_end = int(frame_end_env)
    if frame_start < 1 or frame_end < frame_start:
        raise ValueError(f"invalid requested frame range: {frame_start}..{frame_end}")
    scene.frame_start = frame_start
    scene.frame_end = frame_end
cam = scene.camera or next((o for o in scene.objects if o.type == "CAMERA"), None)
# ★★相機是「有動畫的」，所以「哪一格」必須是明確決定★★(2026-08-09)
#   forest 的 camera_0_0 掛在 camrig.0 上、有 camrig.0Action(frame 1~72)：
#       frame  1 → (-11.61, 66.77, 7.18)   ← ★render_view 出圖用的就是這一格★
#       frame 72 → ( -4.93, 64.26,  3.88)  ← blend 存檔時停在這裡，相差 7.9m
#   ⇒ ★以 frame 1 為準★，因為那是最終成圖的那一格。擺放與出圖用不同格
#     = 東西擺在一台不會被用來出圖的相機前面。
CAM_FRAME = int(scene.frame_start)
scene.frame_set(CAM_FRAME)
bpy.context.view_layer.update()
# ⚠️ ★.copy() 不可省★ —— `matrix_world.translation` 回傳的是★對那個矩陣的 view★，
#    不是值。depsgraph 之後被重新求值(compute_inview 會掃整條相機軌跡)時，
#    這塊記憶體會★就地變成別一格的值★，campos 靜默漂掉而且完全無聲。
#    2026-08-09 就是這樣：add 的取樣其實用的是 frame 72 的相機，
#    自我檢查說「畫面內 4/4」，出圖卻是 frame 1 ⇒ 一隻都看不到。
#    ★本專案第 5 次被 matrix_world 的共享/過期記憶體坑到★(前 4 次見 0806 八節)。
_cam_mw = cam.evaluated_get(
    bpy.context.evaluated_depsgraph_get()).matrix_world.copy()
campos = _cam_mw.translation.copy()
# 相機朝向(-Z 是 Blender 的相機正前方)。給 camera_focus_selection 的視錐用。
camfwd = -Vector((_cam_mw[0][2], _cam_mw[1][2], _cam_mw[2][2])).normalized()

# ★★「乾淨 coarse」護欄★★
#   防的是：unique_assets:X 存在 = X 已經被 populate 過，而 infinigen 的 populate_all
#   遇到已實現的 collection ★會直接跳過、不報錯★ ⇒ 替換安靜地什麼都沒發生。
#
#   ⚠️ ★2026-08-05 收窄★：原本「場景裡有任何 unique_assets: 就一票否決」判定太寬。
#      compose_nature 的 46 個 stage 裡有 2 個★在 coarse 就自己 populate 掉★
#      (populate_collection 全檔只出現 2 次)：
#          fish_school → FishSchoolFactory(BoidSwarmFactory)   水下
#          bug_swarm   → AntSwarmFactory(BoidSwarmFactory)     ★陸地也有★
#      它們是「一個 placeholder = 一整群」的 boid swarm，天生就這樣，不是髒。
#      結果三個水下場景全被擋死，陸地場景只要有蟲群也會中 ⇒ 等於封死所有編輯。
#
#   真正該問的是「★還有 box 沒長出來、卻已經有 unique_assets★」——
#   那才是「populate 會跳過而 box 留在原地」的情形。swarm 的 placeholder 已被消耗成
#   0 個，沒有東西等著長，所以無害。
_realized = {c.name.split(":", 1)[1] for c in bpy.data.collections
             if c.name.startswith("unique_assets:") and len(c.objects) > 0}
_pending = {c.name.split(":", 1)[1] for c in bpy.data.collections
            if c.name.startswith("placeholders:") and len(c.objects) > 0}
dirty = sorted(_realized & _pending)
if _realized - _pending:
    print(f"[plan] ℹ️  coarse 階段就已實現(swarm 之類，非髒): "
          f"{sorted(_realized - _pending)}", flush=True)
#   ★fine 場景的例外★(2026-09-09)：這個護欄防的是「populate 會跑、而且會安靜
#   跳過」。但 scatter 是 GN，★根本不經過 populate★(不產生 placeholder，
#   report 的 needs_populate 也是 False) —— 拿已 populate + fine_terrain 的
#   場景來驗「草有沒有陷進地裡」時，這裡會先把整個測試擋死。
#   放行的條件收得很緊：只有 regression 模式、沒有 hero 編輯、而且每一筆
#   extra 都是 catalog 裡的 scatter 系統時才跳過，production plan 進不來。
_scatter_only = (
    not hero_edits
    and bool(extra_edits)
    and all(sc.get(item.get("target")) is not None for item in extra_edits)
)
if dirty and os.environ.get("BI_SCATTER_REGRESSION") == "1" and _scatter_only:
    print(f"[plan] ⚠️  來源已 populate({dirty})，但這個 plan 只有 scatter 編輯"
          f"(不經過 populate) ⇒ 放行。★這只在 BI_SCATTER_REGRESSION=1 成立★",
          flush=True)
else:
    assert not dirty, (
        f"[plan] 來源不是乾淨的 coarse：{dirty} 已有 unique_assets 卻還留著 box，"
        f"populate 會安靜跳過它們")

from infinigen.core.placement import placement as _pl
from infinigen.core.util import blender as butil

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # BeyondInfinigen/
import placement_modes as pm                                       # noqa: E402
import world_motion as wm                                          # noqa: E402
import local_animation as la                                       # noqa: E402
import placement_contract as plc                                   # noqa: E402

# ★地形選取交給 placement_modes(唯一來源)★ —— 這裡以前寫死 inview_near，
# 但它是只覆蓋鏡頭前一小塊的 LOD 分塊，範圍外的 ray_cast 全部打空 ⇒ 靜默不貼合。
terrain = pm.terrain_candidates() or None
print(f"[plan] 貼地地形候選: {[o.name for o in terrain] if terrain else '(無)'}", flush=True)

# ⚠️ ★scatter 的 apply() 要的是單一物件，而且必須維持原本的選法★ ——
#    它決定 GN 把東西撒在哪一個網格上，換一個會直接改變散佈結果(不是 bug 是行為改變)。
#    貼地幾何用上面那份候選清單，這個只給 EXTRA create 用，兩者不要混。
# ★scatter 基底可切換★(2026-09-08)
#   inview_near 是 LOD 分塊，實測 forest/seed0 只覆蓋離相機 1.4–22.1m；
#   而 fine 場景裡★可見地面全部在 23m 之外★(量過 6/6 格)，於是那些地方
#   永遠不會有東西 —— 不是遮罩沒選中，是那段地面★不在這張網格上★。
#   切換用 SCATTER_BASE=terrain 改用整片 OpaqueTerrain(1.4–166m)，
#   範圍換來的浪費用 camera_far_m 收掉。預設維持舊行為。
#   ★2026-09-08 翻預設★：實測 16 倍實例數(679 → 10879)的 render 耗時
#   13.24s → 8.94s、Peak 記憶體 6775M → 6779M —— ★沒有代價★(GN instancing
#   共用幾何，成本在建 12 個母本不在複製幾份)。populate/fine_terrain 完全
#   不受影響(scatter 不產生 placeholder，fine_terrain 只重建地形網格)。
#   SCATTER_BASE=inview_near 可退回舊行為。
#   ★2026-09-09 改成照官方逐支選★ —— generate_nature.py 的 run_stage 第三個參數
#   就是每一支自己的 target：flowers/ferns/grass/pebbles/corals... 用
#   terrain_inview，ground_leaves/ground_twigs/mushroom/pinecone/seashells/
#   pine_needle/decorative_plants 用 terrain_near。catalog 逐支記在
#   terrain_target。SCATTER_BASE 可覆寫(terrain=整片、near/inview=指定分塊)。
_SCATTER_BASE = os.environ.get("SCATTER_BASE", "").strip().lower()
_BASE_OBJ = {
    "near":    "OpaqueTerrain.inview_near",      # 官方 terrain_near
    "inview":  "OpaqueTerrain.inview_inview",    # 官方 terrain_inview
    "terrain": "OpaqueTerrain",
}


def pick_scatter_base(target_name):
    """依 catalog 記下的官方 target 選基底物件；找不到就逐級退回。"""
    want = _SCATTER_BASE or (sc.official_mask_params(target_name).get(
        "terrain_target") or "inview")
    order = [_BASE_OBJ.get(want, _BASE_OBJ["inview"]),
             _BASE_OBJ["inview"], _BASE_OBJ["near"], _BASE_OBJ["terrain"]]
    for name in order:
        obj = next((o for o in bpy.data.objects if o.name == name), None)
        if obj is not None:
            return obj, want
    return None, want


terrain_scatter = next(
    (o for o in bpy.data.objects if o.name == _BASE_OBJ["inview"]), None) or \
    next((o for o in bpy.data.objects if o.name == _BASE_OBJ["terrain"]), None)

# ★MaskTag.json★ —— density.placement_mask(tag=...) 的字典來源(terrain 寫出來的)。
#   coarse 產物會跟 scene.blend 放在同一層，step 8 把整個 coarse/ 除了 blend 都複製
#   進 work/，所以就在 IN_BLEND 旁邊。找不到就退成「不套 domain」而不是硬猜 tag。
def _load_scatter_tag_dict():
    # ⚠️ ★不能只看 IN_BLEND 旁邊★(2026-09-07 e2e 實測)：step 8 的輸入是
    #    0_camera/baked_coarse.blend，那裡沒有 MaskTag.json；真正帶著 coarse
    #    metadata 的是 ★OUT_BLEND 所在的 work/★(step 8 把 coarse 除了 blend
    #    以外全部複製過去)。只找 IN_BLEND 會靜靜退成「不套 domain」，
    #    scatter 就撒滿整片地形(含畫面外)。
    roots = [Path(IN_BLEND).resolve().parent]
    if not DRY_RUN:
        roots.append(Path(OUT_BLEND).resolve().parent)
    seen = set()
    for root in roots:
        for candidate in (root / "MaskTag.json", root.parent / "MaskTag.json"):
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate.is_file():
                try:
                    return json.loads(candidate.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    continue
    return None


_scatter_tag_dict = _load_scatter_tag_dict()


def stamp_anchor(o, anchoring, yaw):
    """★階段 A → 階段 C 的橋★：把 anchoring 與★原始 yaw★寫進 box 的 custom property。

    ⚠️ yaw 一定要另外存 —— place() 把 box 轉斜之後 rotation_euler.z ★就不再是 yaw★
       (它變成三軸複合旋轉的一個分量)，階段 C 讀不回來。沒有它就沒辦法在 populate
       之後用真幾何重算一次沉降。
    ⚠️ 旋轉本身不需要走這座橋 —— 它已經烤進 box 的 rotation_euler，
       populate 的 parent_to(no_inverse=True) 會自動繼承。
    """
    if anchoring:
        o["bi_anchoring"] = anchoring
        o["bi_yaw"] = float(yaw)

report = {"in_blend": IN_BLEND, "out_blend": None if DRY_RUN else OUT_BLEND,
          "plan": PLAN_JSON, "dry_run": DRY_RUN,
          "hero_results": [], "extra_results": [],
          "needs_populate": False}
added_names = []
# ★代理佔位的意圖★: box 名 → 它其實要長成的 factory(2026-07-29)
#   場景沒有某個 factory 時，add 會借用代理 box 佔位；
#   ★這張表把「這個 box 其實要變成什麼」記下來★，寫進 manifest 給階段 B/C。
proxy_intent = {}
# Exact collection -> asset-generator id. Replacement attrs stamp every collection in
# the requested factory category; additions stamp only their newly created collection.
collection_code_intent = {}


# ════════════════════════════════════════════════════════ 共用
def colls_for(factory):
    return [(c, int(m.group(3))) for c in bpy.data.collections
            if (m := PH_COLL_RE.fullmatch(c.name)) and m.group(2) == factory]


def roots_of(coll):
    return [o for o in coll.objects if o.parent is None]


def _in_camera_view(p):
    """世界座標 p 在不在★blend 裡那台相機★的畫面內(frame CAM_FRAME)。

    ⚠️ 這支★只給 explicit positions 用★ —— 它讀的是場景現況的相機。
       取樣那條路自己有一整套(會套 CAM_VIEW 再還原)，不要混用。
    """
    from bpy_extras.object_utils import world_to_camera_view
    scene.frame_set(CAM_FRAME)
    bpy.context.view_layer.update()
    co = world_to_camera_view(scene, cam, Vector(p))
    return 0.0 < co.x < 1.0 and 0.0 < co.y < 1.0 and co.z > 0


def sample_add_positions(n_add, radius, href, min_sep=2.5, min_clear=2.0, tries_per=80,
                         screen_positions=None, screen_bounds=None,
                         screen_yaws=None,
                         screen_projection="terrain", allow_projection_misses=False,
                         surface_policy=None, footprint_half_extents=(1.0, 1.0),
                         box_half_height=1.0):
    """★add 的落點：在畫面座標裡取樣，不是在相機前方的盒子裡亂撒。★(2026-08-09)

    ★舊做法為什麼不對★(2026-08-09 量出來的)：
        pos = campos + fwd*(8+U(0,radius)) + right*U(-radius/2, +radius/2)
      橫向偏移是★與距離無關的常數★。radius=10 時最近處 8m 配 ±5m = ★離軸 32°★，
      而 forest 這台相機的水平半視角只有 ★17.7°★(垂直更只有 10.2°)
      ⇒ 規則允許的取樣範圍★有一大半天生在畫面外★。
      實測 e2e_peacock_candy 的 4 隻孔雀：水平 -21°~-37°(界 ±17.7°)★4/4 出界★、
      垂直 -1.6°~-27.4°(界 ±10.2°)★3/4 出界★ —— 而且 radius=10 是 composer 隨手給的
      預設值(它自己在 warnings 裡寫「plan 沒給 radius」)，★沒有人拿相機視角算過★。
      結果就是「近拍看得到、主視角一隻都看不到」。

    ★新做法★：從相機往★畫面內的一點★射線，打到★地形★的交點就是落點。
      在畫面內這件事因此是★定義出來的★，不是碰運氣的。

    ⚠️ ★射線只能打地形，不能打整個場景★ —— 第一版用 scene.ray_cast 是錯的：
       coarse 裡每一條射線都先打到 TreeFactory/BushFactory 的 ★2m placeholder
       立方體★，實測 80 次嘗試 ★100% 被擋、一條都到不了地形★，四個點全退回舊規則。
       但那些立方體★不是最終幾何★ —— 真正的樹幹是細的，樹與樹之間的地面本來就
       看得見(對照 cherry_apple_forest.png)。把 placeholder 當實心牆，
       等於把整片森林當成一堵牆。

    遮擋改用★不要落在既有 placeholder 身上★來判(XY 距離 min_clear)。
    coarse 階段只知道「那裡會長出一棵樹」、不知道它的剪影，這是唯一講得通的近似。

    ⚠️ ★邊緣要留白★ —— NDC 取 x∈[0.15,0.85] y∈[0.2,0.65]：物件有體積，中心點剛好
       壓在畫面邊界的話身體會被切一半；y 偏下是因為要站在地面上。
    ⚠️ ★失敗要出聲並退回舊規則★ —— 寧可擺在畫面外也不要一個都不擺(下游 populate
       的數量會對不上)，但★必須印出來★，不能靜默。
    """
    # ★不要相信外面傳進來的相機狀態★ —— 相機有動畫，任何人只要 frame_set 過就會漂。
    #   在這裡★自己把格數釘回 CAM_FRAME★ 並就地重算，位置與基底一定同一格。
    scene.frame_set(CAM_FRAME)
    bpy.context.view_layer.update()

    # ★套上「出圖用的那台相機」★(CAM_VIEW，見檔頭)。用完★一定要還原★ ——
    #   render_view 之後會從原始 base 再套一次自己的偏移，這裡若留著就會疊兩次。
    _restore = None
    if CAM_VIEW_ON:
        _rig = cam
        while _rig.parent is not None:
            _rig = _rig.parent
        _saved = (_rig.location.copy(), _rig.rotation_euler.copy(),
                  cam.rotation_euler.copy(), _rig.rotation_mode, cam.rotation_mode)

        def _restore():
            _rig.location, _rig.rotation_euler = _saved[0], _saved[1]
            cam.rotation_euler = _saved[2]
            _rig.rotation_mode, cam.rotation_mode = _saved[3], _saved[4]
            bpy.context.view_layer.update()

        _base_rot_z = cam.rotation_euler.z
        _rig.location.z += CAM_VIEW["aerial"]
        cam.rotation_mode = "XYZ"
        cam.rotation_euler = (math.radians(90 - CAM_VIEW["pitch"]), 0,
                              _base_rot_z + math.radians(CAM_VIEW["yaw"]))
        _rig.rotation_mode = "XYZ"
        _rig.rotation_euler = (0, 0, _rig.rotation_euler.z)
        bpy.context.view_layer.update()
        _d = bpy.context.evaluated_depsgraph_get()
        _m = cam.evaluated_get(_d).matrix_world
        _f = -Vector((_m[0][2], _m[1][2], _m[2][2])).normalized()
        _r = Vector((_m[0][0], _m[1][0], _m[2][0])).normalized()
        _rig.location += -_f * CAM_VIEW["back"] + _r * CAM_VIEW["side"]
        bpy.context.view_layer.update()
        print(f"[hero]   ★套用出圖相機★ {CAM_VIEW}", flush=True)

    deps = bpy.context.evaluated_depsgraph_get()
    mw = cam.evaluated_get(deps).matrix_world.copy()
    campos = mw.translation.copy()
    fwd = -Vector((mw[0][2], mw[1][2], mw[2][2])).normalized()
    right = Vector((mw[0][0], mw[1][0], mw[2][0])).normalized()
    up = Vector((mw[0][1], mw[1][1], mw[2][1])).normalized()
    tan_x, tan_y = math.tan(cam.data.angle_x / 2), math.tan(cam.data.angle_y / 2)
    print(f"[hero]   取樣相機 = frame {CAM_FRAME} @ "
          f"{tuple(round(v,2) for v in campos)}", flush=True)
    # ★距離窗要逐步放寬，不能拿 radius 當硬約束★(2026-08-09，snowy_mountain 打臉)
    #   `8+radius` 是從舊規則抄來的，而 radius 是 composer 隨手給的預設值 10。
    #   forest 的相機在 z=7.2、地面就在腳下 ⇒ 命中都落在 6~16m，剛好蒙混過去。
    #   ★snowy_mountain 的相機在 z=15.05 的高處俯瞰山谷★ ⇒ 畫面內的地面大多在 18m
    #   之外，每一條射線都在碰到地面前就被距離上限砍掉：
    #       四筆 add 全部「射線打不到地形」240~960 次 ⇒ 全退回舊規則 ⇒ 畫面內 0/N。
    #   ⇒ 跟間距同理：★「在畫面內」是硬要求，「多遠」是可以讓的★。
    d_windows = [(5.0, 8.0 + radius), (5.0, 3 * (8.0 + radius)), (2.0, 1e9)]

    # 既有 placeholder 的 XY —— 用來避開「長在樹/灌木身上」
    occupied = [o.matrix_world.translation
                for c in bpy.data.collections if PH_COLL_RE.fullmatch(c.name)
                for o in c.objects if o.parent is None]

    def terrain_hit(direction):
        """★只對地形做射線★(物件層 ray_cast，不理會場景裡其他東西)。"""
        best = None
        for t in (terrain or []):
            ev = t.evaluated_get(deps)
            mwi = t.matrix_world.inverted()
            o_l = mwi @ campos
            d_l = (mwi.to_3x3() @ direction).normalized()
            try:
                # ⚠️ ★這裡不設距離上限★ —— 由外面的 d_windows 決定收不收。
                #    先前把上限綁在 d_max(=8+radius)，在開闊地形會讓射線在碰到
                #    地面之前就被砍掉，回報「打不到地形」而其實是「打得到但太遠」。
                ok, loc_l, _n, _idx = ev.ray_cast(o_l, d_l, distance=1e5)
            except Exception:
                continue
            if not ok:
                continue
            loc_w = t.matrix_world @ loc_l
            dist = (loc_w - campos).length
            if best is None or dist < best[0]:
                best = (dist, loc_w)
        return best

    def ground_z(p):
        if terrain is not None:
            _n, _gz = pm.support_normal(terrain, p, 0.0)
            if _gz is not None:
                return _gz
        return (min(href, key=lambda o: (o.location.xy - p.xy).length).location.z
                if href else p.z)

    out = []
    support_surfaces = []
    support_normals = []
    # ★取樣失敗要能說出為什麼★ —— 「射線打不到地形」有好幾種完全不同的原因
    #   (地形在 depsgraph 裡被藏起來 / 前面全是 placeholder box / 距離超界)，
    #   不記下來的話只會看到「退回舊規則」而不知道要修哪個。
    from collections import Counter
    why = Counter()
    requested_screen = list(screen_positions or [])
    if requested_screen and len(requested_screen) != n_add:
        raise ValueError(
            f"screen_positions 必須剛好有 {n_add} 點，收到 {len(requested_screen)}")
    requested_yaws = None if screen_yaws is None else list(screen_yaws)
    if requested_yaws is not None:
        if len(requested_yaws) != n_add:
            raise ValueError(
                f"placement_yaws 必須剛好有 {n_add} 個，收到 {len(requested_yaws)}")
        if any(not isinstance(yaw, (int, float)) or not math.isfinite(float(yaw))
               for yaw in requested_yaws):
            raise ValueError("placement_yaws 必須全是有限數值")
        requested_yaws = [float(yaw) for yaw in requested_yaws]
    xmin, ymin, xmax, ymax = map(
        float, screen_bounds or (0.15, 0.20, 0.85, 0.65))
    surface_policy = plc.normalize_surface_policy(surface_policy)

    # Canonical placement and the planning map share this exact evaluator.  It
    # checks one submitted bottom-left-origin screen coordinate and never
    # searches for or substitutes another coordinate.
    from placement_surface_evaluator import VisibleSurfaceEvaluator
    visible_evaluator = VisibleSurfaceEvaluator(
        scene=scene,
        camera=cam,
        scene_name=surface_scene_name,
        origin=campos,
        forward=fwd,
        right=right,
        up=up,
        tan_x=tan_x,
        tan_y=tan_y,
        surface_policy=surface_policy,
        footprint_half_extents=footprint_half_extents,
        box_half_height=box_half_height,
    )

    def _project_visible(nx, ny, yaw=0.0):
        evaluation = visible_evaluator.evaluate(nx, ny, yaw=float(yaw))
        return evaluation.direction, evaluation.hit, evaluation.reason

    # Composer owns submitted coordinates. Validate every point before making
    # placeholder mutations; the executor must not silently replace rejected
    # points with deterministic probes.
    if requested_screen and screen_projection == "visible_surface" \
            and not allow_projection_misses:
        projection_failures = []
        for instance_index, submitted in enumerate(requested_screen):
            nx, ny = map(float, submitted)
            planned_yaw = (requested_yaws[instance_index]
                           if requested_yaws is not None else 0.0)
            _direction, result, issue = _project_visible(
                nx, ny, planned_yaw)
            if result is not None and issue is None:
                continue
            projection_failures.append({
                "instance_index": instance_index,
                "submitted_screen_position": [nx, ny],
                "screen_bounds": [xmin, ymin, xmax, ymax],
                "failure": "raycast_miss" if result is None else "projection_rejected",
                "reason": ("camera ray did not hit a visible surface"
                           if result is None else str(issue)),
                "surface_policy": surface_policy,
                "hit_surface": None if result is None else str(result[2]),
            })
        if projection_failures:
            raise plc.PlacementProjectionFailure(projection_failures)
    for add_index in range(n_add):
        got = None
        note = ""
        if requested_screen:
            nx, ny = map(float, requested_screen[add_index])
            # 擴大網格開著時，搜尋範圍就是 2x2 畫面 ⇒ 座標本來就可能在畫面外
            _limits = plc.extended_search_bounds() or [0.0, 0.0, 1.0, 1.0]
            if not (_limits[0] <= nx <= _limits[2] and _limits[1] <= ny <= _limits[3]):
                raise ValueError(
                    f"screen_positions[{add_index}] 超出搜尋範圍 {_limits}: {[nx, ny]}")
            on_screen = 0.0 < nx < 1.0 and 0.0 < ny < 1.0
            # ★後面要讀 result[2]/result[3] 的條件是「真的走了可見表面投影」★，
            #   不是「要求的模式是可見表面」——畫面外的點退回地形投影，
            #   terrain_hit 只回 2 元組(0911 arctic: IndexError 讓整個 case 死)。
            used_visible = screen_projection == "visible_surface" and on_screen
            if used_visible:
                planned_yaw = (requested_yaws[add_index]
                               if requested_yaws is not None else 0.0)
                direction, result, policy_issue = _project_visible(
                    nx, ny, planned_yaw)
            else:
                direction = (
                    right * ((nx * 2 - 1) * tan_x)
                    + up * ((ny * 2 - 1) * tan_y) + fwd
                ).normalized()
                result = terrain_hit(direction)
                policy_issue = None
            if policy_issue:
                why[policy_issue] += 1
                if not allow_projection_misses:
                    result = None
            if result is None:
                if allow_projection_misses:
                    # A temporary candidate sheet may include sky/no-hit points.
                    # Keep a dummy point on the exact ray so it can be labeled;
                    # the missing surface is reported and makes it unselectable.
                    got = campos + direction * 20.0
                    support_surfaces.append(None)
                    support_normals.append(None)
                    note = (f" ★候選畫面座標★ ({nx:.3f}, {ny:.3f})"
                            " → 無可見表面（僅標示、不可選）")
                    result = None
                else:
                    if _restore is not None:
                        _restore()
                    target_kind = "可見表面" if used_visible else "地形"
                    raise RuntimeError(
                        f"screen_positions[{add_index}]={[nx, ny]} 的相機射線打不到{target_kind}")
            if result is None:
                pass
            else:
                got = Vector(result[1])
                support_surfaces.append(str(result[2]) if used_visible else None)
                support_normals.append(Vector(result[3]) if used_visible else None)
                note += (f" ★critic 指定畫面座標★ ({nx:.3f}, {ny:.3f})"
                        + (f" → 可見表面 {result[2]}" if used_visible
                           else "" if on_screen else " → ★畫面外★，改用地形投影"))
        # ★逐步放寬★ —— 「在畫面內」是硬要求，「多遠」與「間距」都是可以讓的。
        #   兩個維度都要放：森林是可見地面零碎(間距放不下)，開闊山景是地面太遠
        #   (距離窗放不下)。只放一個維度就會在另一種地形上退回舊規則。
        for (d_min, d_max) in ([] if got is not None else d_windows):
            for relax in (1.0, 0.5, 0.0):
                # ★收集候選再取最近的，不要拿第一個★(2026-08-09)
                #   「在畫面內」與「夠近」是兩個目標，只顧前者會得到 170m 外的禮物盒
                #   (snowy_mountain 實測：距離窗放到無上限才有地形，第一個命中 151m，
                #    2m 的物件在那個距離只剩十幾個像素 = 等於沒放)。
                #   ⚠️ 舊規則反而意外給了好的尺度(寫死 8~18m)，它錯在不管畫面 ——
                #     所以修法是★兩個目標一起顧★，不是換一個目標。
                cands = []
                for _t in range(tries_per):
                    nx = random.uniform(xmin, xmax)
                    ny = random.uniform(ymin, ymax)
                    d = (right * ((nx * 2 - 1) * tan_x)
                         + up * ((ny * 2 - 1) * tan_y) + fwd).normalized()
                    res = terrain_hit(d)
                    if res is None:
                        why["射線打不到地形"] += 1
                        continue
                    dist, loc = res
                    if not (d_min <= dist <= d_max):
                        why[f"距離 {dist:.0f}m 不在 {d_min:.0f}~{d_max:.0f}m"] += 1
                        continue
                    if relax and any((loc.xy - q.xy).length < min_clear * relax
                                     for q in occupied):
                        why["落在既有 placeholder 上"] += 1
                        continue
                    if relax and any((loc.xy - q.xy).length < min_sep * relax
                                     for q in out):
                        why["離已擺的太近"] += 1
                        continue              # ★彼此也會擋★，拉開一點
                    cands.append((dist, Vector(loc)))
                    if len(cands) >= 12:      # 夠挑就好，不必把 80 次跑完
                        break
                if cands:
                    got = min(cands, key=lambda c: c[0])[1]
                    if relax < 1.0:
                        note += f" 間距放寬到 {relax:.0%}"
                    note += f" (從 {len(cands)} 個候選取最近)"
                    break
            if got is not None:
                if (d_min, d_max) != d_windows[0]:
                    note += f" 距離窗放寬到 {d_min:.0f}~{d_max:.0f}m"
                break
        if note:
            print(f"[hero]   ℹ️{note} 才找到畫面內的落點", flush=True)
        if got is None:
            # Formal additions must be visible.  The old fallback sampled a point
            # in a world-space box, knowingly allowed it outside the frustum, and
            # still let the E2E turn green.  Stop at the cheap placement stage.
            if _restore is not None:
                _restore()
            raise RuntimeError(
                f"找不到最終相機畫面內的地形落點；原因前三: {why.most_common(3)}")
        # ``got`` is already the exact camera-ray/terrain intersection.  Replacing
        # its Z with a second support query can move it off that ray and therefore
        # outside the camera (the 0812 desert eagle failure).
        _v = got - campos
        print(f"[hero]   · 落點 dist={_v.length:5.1f}m  "
              f"離軸 水平{math.degrees(math.atan2(_v.dot(right), _v.dot(fwd))):+6.1f}°"
              f" 垂直{math.degrees(math.atan2(_v.dot(up), _v.dot(fwd))):+6.1f}°"
              f"  (界 ±{math.degrees(cam.data.angle_x)/2:.1f}° / ±"
              f"{math.degrees(cam.data.angle_y)/2:.1f}°)  "
              f"{'⛔出界' if _off_screen(got, campos, fwd, right, up, tan_x, tan_y) else '✅'}",
              flush=True)
        out.append(got)
        if len(support_surfaces) < len(out):
            support_surfaces.append(None)
        if len(support_normals) < len(out):
            support_normals.append(None)
    n_ok = n_add - sum(1 for i, p in enumerate(out) if _off_screen(p, campos, fwd, right,
                                                                  up, tan_x, tan_y))
    if _restore is not None:
        _restore()          # ★把相機還原★，否則 render_view 會再疊一次偏移
    return out, n_ok, support_surfaces, support_normals


def _off_screen(p, campos, fwd, right, up, tan_x, tan_y):
    v = p - campos
    fz = v.dot(fwd)
    if fz <= 0:
        return True
    return abs(v.dot(right) / fz) > tan_x or abs(v.dot(up) / fz) > tan_y


def compute_inview():
    """⚠️ ★會動到 frame，跑完必須還原★(2026-08-09)

    filter_populate_targets 是沿★整條相機軌跡★判斷的，內部會 frame_set。
    它跑完之後 scene 停在最後一格(72)，而相機是有動畫的 ⇒ 之後任何「讀相機位置」
    的程式碼都會拿到 frame 72 的相機，★而且沒有任何跡象★。
    """
    cams = [o for o in bpy.data.objects if o.type == "CAMERA"]
    names = set()
    for coll in bpy.data.collections:
        match = PH_COLL_RE.fullmatch(coll.name)
        if not match:
            continue
        cull = pm.populate_cull_for(match.group(2))
        try:
            rows = php.population_target_metadata(
                roots_of(coll),
                cams,
                dist_cull=cull.get("dist_cull", 200),
                vis_cull=cull.get("vis_cull", 0),
                include_all=False,
            )
            names |= {
                name for name, row in rows.items()
                if row.get("will_populate")
            }
        except Exception:
            pass
    scene.frame_set(CAM_FRAME)              # ★還原★
    bpy.context.view_layer.update()
    return names


if any(edit.get("op") in {"delete", "scale", "move"} for edit in hero_edits):
    INVIEW = compute_inview()
else:
    INVIEW = set()
print(f"[plan] populate 看得到的 placeholder: {len(INVIEW)}", flush=True)


def camera_focus_selection(base_selection, cam_location, cam_forward,
                           near_m, far_m, cos_half_angle):
    """把 selection 限制在「相機看得到、而且看得清楚」的那一段地形。

    ★為什麼需要★(2026-09-08)：scatter 的遮罩是純世界座標的(noise 團塊 x 坡度 x
    MaskTag)，★沒有任何一項知道畫面★。catalog 的 domain tag 雖然附加了
    -out_of_view，但實測 step 8 用的 ★coarse★ MaskTag 根本沒有 out_of_view 這種
    key(35 個 key 裡一個都沒有)，那個過濾形同無效。結果是把實例平均撒滿整片地形，
    畫面內只分到一小部分，遠處那些又小又被遮住。

    這裡加兩個條件，兩個都是可選的(給 None 就不加)：
      距離帶  near < |p - cam| < far     太近會糊在鏡頭上，太遠讀不出來
      視錐    normalize(p - cam) · fwd > cos(half_angle)
              半角建議取相機水平半視角再放寬一點，讓邊緣不要硬切
    """
    def selection(nw):
        from infinigen.core import surface as _surface
        from infinigen.core.nodes.node_info import Nodes
        # ⚠️ ★placement_mask(return_scalar=True) 回的是 (mask, 漸變係數) tuple★
        #   (2026-09-09)。那個係數是 scatter_instances 的 taper_density /
        #   taper_scale 用來把斑塊邊緣柔化的東西 —— 官方十支裡六支開著。
        #   這裡如果只取 mask、把 tuple 拆掉，柔邊就會靜靜消失(不會報錯，
        #   只是邊界又變回硬切)。⇒ 把係數原樣帶回去。
        base = _surface.eval_argument(nw, base_selection)
        taper = None
        if isinstance(base, tuple):
            mask, taper = base[0], base[1]
        else:
            mask = base
        position = nw.new_node(Nodes.InputPosition)
        campos = tuple(float(v) for v in cam_location)
        if near_m is not None or far_m is not None:
            distance = nw.new_node(
                Nodes.VectorMath, input_kwargs={0: position, 1: campos},
                attrs={"operation": "DISTANCE"})
            if near_m is not None:
                far_enough = nw.new_node(
                    Nodes.Compare,
                    attrs={"operation": "GREATER_THAN", "data_type": "FLOAT"},
                    input_args=[distance.outputs["Value"], float(near_m)])
                mask = nw.scalar_multiply(mask, far_enough)
            if far_m is not None:
                near_enough = nw.new_node(
                    Nodes.Compare,
                    attrs={"operation": "LESS_THAN", "data_type": "FLOAT"},
                    input_args=[distance.outputs["Value"], float(far_m)])
                mask = nw.scalar_multiply(mask, near_enough)
        if cos_half_angle is not None and cam_forward is not None:
            to_point = nw.new_node(
                Nodes.VectorMath, input_kwargs={0: position, 1: campos},
                attrs={"operation": "SUBTRACT"})
            unit = nw.new_node(
                Nodes.VectorMath, input_kwargs={0: to_point},
                attrs={"operation": "NORMALIZE"})
            dot = nw.new_node(
                Nodes.VectorMath,
                input_kwargs={0: unit,
                              1: tuple(float(v) for v in cam_forward)},
                attrs={"operation": "DOT_PRODUCT"})
            in_cone = nw.new_node(
                Nodes.Compare,
                attrs={"operation": "GREATER_THAN", "data_type": "FLOAT"},
                input_args=[dot.outputs["Value"], float(cos_half_angle)])
            mask = nw.scalar_multiply(mask, in_cone)
        return (mask, taper) if taper is not None else mask

    return selection


def camera_excluded_selection(base_selection, cam_location, radius_m):
    """把 selection 再乘上「距相機 radius_m 公尺內不撒」的遮罩。

    ★為什麼 infinigen 沒有這個★：官方是★先撒再擺相機★，撞不到這個問題。
    我們是相機已經固定、往既有場景加東西，scatter 會照撒不誤 —— 0907 實測
    向日葵直接長在鏡頭前，整張圖變成一道花牆(diag_noflower.png 可對照：
    只把花藏起來，畫面立刻恢復)。

    幾何：position 到相機的距離 > radius 才保留。距離節點是
    VectorMath/DISTANCE，★純量在 outputs["Value"]★(不是 Vector)。
    """
    def selection(nw):
        # ⚠️ 延遲 import：apply_plan 的模組層級沒有 infinigen 的 Nodes，
        #    而這支只在 Blender 裡被呼叫。
        from infinigen.core import surface as _surface
        from infinigen.core.nodes.node_info import Nodes
        mask = _surface.eval_argument(nw, base_selection)
        position = nw.new_node(Nodes.InputPosition)
        distance = nw.new_node(
            Nodes.VectorMath,
            input_kwargs={0: position, 1: tuple(float(v) for v in cam_location)},
            attrs={"operation": "DISTANCE"})
        far_enough = nw.new_node(
            Nodes.Compare,
            attrs={"operation": "GREATER_THAN", "data_type": "FLOAT"},
            input_args=[distance.outputs["Value"], float(radius_m)])
        return nw.scalar_multiply(mask, far_enough)

    return selection


def ground_aligned_factory(FacClass):
    """把 overlay factory 包成「底部在 z=0」的版本，給 scatter 當母本用。

    ★為什麼非包不可★(2026-09-07 實測)：
      infinigen 的 scatter 母本契約是★原點在底部、幾何往上長★ ——
          FlowerPlantFactory   z ∈ [-0.008, +0.995]
      但 hero 資產不守這個慣例，例如庫裡的向日葵 FlowerFactory：
          FlowerFactory        z ∈ [-0.78,  +0.037]   ← 花在原點，莖往下垂
      scatter_instances 用 ground_offset=0 把★原點★放在地表，於是整根 78cm 的
      莖埋進土裡，只剩花盤平貼在地上(0907 smoke 的第一版 render 就是這樣)。
      ⇒ 換外觀時一律把母本幾何平移到底部齊 0，讓它符合 scatter 的契約。

    ⚠️ 平移寫進★mesh 頂點座標★而不是 obj.location —— instancer 怎麼讀物件變換
       依 GN 節點設定而異，改頂點是唯一無歧義的做法。
    """
    # ⚠️ ★不可以用繼承包★(2026-09-10 實測炸掉 grass←silvergrass)：
    #    生成資產常用 `type(self) is X` 當子類別防遞迴守衛，例如 overlay 的
    #        if type(self) is GrassesMonocotFactory:
    #            self.n_plumes = np.random.randint(7, 11)
    #    (讓 Wheat/Maize/Reed 這些子類別不要長銀穗)。包成子類別之後
    #    type(self) 變成 GroundAlignedGrassesMonocotFactory ⇒ 守衛不成立 ⇒
    #    n_plumes 從沒被設 ⇒ create_asset 直接 AttributeError。
    #    ★改成原地覆寫 create_asset★：類別本身不變，type(self) 仍然成立。
    #    overlay 是每個 apply_plan process 各自載入的副本，就地改動不會外溢。
    original = FacClass.create_asset
    if getattr(original, "_bi_ground_aligned", False):
        return FacClass

    def create_asset(self, **kwargs):
        obj = original(self, **kwargs)
        mesh = getattr(obj, "data", None)
        verts = getattr(mesh, "vertices", None)
        if obj.type != "MESH" or not verts:
            return obj
        # ★母本縮放★(2026-09-10)：scatter 的密度是
        #     density = vol_density / (avg_vol * avg_scale²)
        # 也就是★除以母本體積★。庫存資產跟官方母本差一個量級就會讓數量崩掉：
        # pine_needle 換成 cattail_reed(1.5m 香蒲當松針)，4,229,476 → ★4 株★。
        # ⚠️ 不能用傳參數的方式救 —— pine_needle / seaweed 的 apply() 雖然
        #    收 density 跟 scale，但★兩個都是死參數★(內部寫死 vol_density=U(...)
        #    與 scale=U(2,3))。唯一通用的槓桿就是母本網格本身。
        # 這個值由 apply_plan 的 contract 迴圈在★量到數量崩潰時★才調降，
        # 平常是 1.0 ⇒ 向日葵那種「本來就比較大朵、數量本來就該少」完全不受影響。
        factor = float(getattr(FacClass, "_bi_master_scale", 1.0) or 1.0)
        if abs(factor - 1.0) > 1e-9:
            for v in verts:
                v.co *= factor
        # 只有繞 Z 的旋轉不影響高度；factory 產物的變換就是這一類。
        low = min(v.co.z for v in verts)
        if abs(low) > 1e-6:
            for v in verts:
                v.co.z -= low
        mesh.update()
        return obj

    create_asset._bi_ground_aligned = True
    FacClass.create_asset = create_asset
    FacClass._bi_master_scale = 1.0
    return FacClass


# ⚠️★不要「修好」butil.delete_collection 的 use-after-free★(2026-09-10 試過，更糟)
#   官方寫法是：
#       objects = collection.objects            # 綁在 collection 上的即時 view
#       bpy.data.collections.remove(collection) # 先釋放 collection
#       for o in objects: delete_collection(o)  # 再走訪 = 存取已釋放記憶體
#   看起來明顯是 bug，但★周邊程式碼依賴這個行為★：迭代已釋放的 view 通常
#   什麼都不吐，於是 delete(o) 根本不會被呼叫，delete_collection 的實際效果
#   就只是「移除 collection、把物件變孤兒」。
#   我把 objects 改成真正的快照之後，迴圈開始真的迭代那些物件並呼叫 delete(o)
#   → select(o) → 但物件已不在 scene(collection 已移除) → bpy.ops.object.delete()
#   要求物件在 scene 中 ⇒ ★從「部分母本失敗」變成「全部母本失敗」★：
#       grass       8/10 成功 → ★0/10★
#       pine_needle 3/3 成功  → ★0/3★
#   ⇒ 母本偶發失敗改用 tolerant_make_asset_collection 容錯，不要動官方這段。


def tolerant_make_asset_collection(original, report, scale=1.0):
    """讓個別母本生成失敗不要炸掉整批。

    ★為什麼需要★(2026-09-10 實測)：grass←silvergrass 的母本池要 10 個，
    第 8 個在官方 growth.py 的清理路徑炸掉
        ValueError: Object o.name='' not in scene and cant be selected
    ⇒ ★已經成功的 7 個被連帶丟掉★，整個 apply 失敗。
    生成資產在 __init__ 裡又開一個 factory(silvergrass 每株含 7~11 根銀穗)時，
    一批 n=10 等於 70~110 次巢狀建構，踩到失效 datablock 的機率不低。
    當 hero 只建 1 次所以沒事，當 scatter 母本要建 10 次就會中。

    降級成功(7 個母本)遠比硬失敗有用。但★全部失敗仍然要拋★ ——
    否則會產生空的母本 collection，撒出 0 個實例而且不報錯。
    """
    import sys as _sys

    factory_mod = _sys.modules[original.__module__]

    def wrapper(spawn_fns, n, name=None, weights=None, as_list=False,
                verbose=True, centered=False, **kwargs):
        fns = spawn_fns if isinstance(spawn_fns, list) else [spawn_fns]
        if weights is None:
            weights = _np.ones(len(fns))
        weights = _np.asarray(weights, dtype=float)
        weights = weights / weights.sum()
        if name is None:
            name = ",".join([repr(f) for f in fns])

        objs = [[] for _ in fns]
        failures = []
        for i in range(n):
            fn_idx = int(_np.random.choice(_np.arange(len(fns)), p=weights))
            try:
                obj = fns[fn_idx](i=i, **kwargs)
            except Exception as error:  # noqa: BLE001
                failures.append(f"{i}: {type(error).__name__}: {error}")
                print(f"[extra]   ⚠️ 母本 {i}/{n} 生成失敗，略過 "
                      f"({type(error).__name__}: {str(error)[:90]})", flush=True)
                continue
            if centered:
                obj.location = -factory_mod.center(obj)
                butil.apply_transform(obj, True)
            objs[fn_idx].append(obj)

        built = sum(len(o) for o in objs)
        if failures:
            report.setdefault("master_spawn_failures", []).extend(failures)
            report["master_spawn_built"] = built
            report["master_spawn_requested"] = n
        if built == 0:
            raise RuntimeError(
                f"母本池 {name} 要 {n} 個，★全部生成失敗★：{failures[:3]}")

        for os, f in zip(objs, fns):
            if hasattr(f, "finalize_assets"):
                f.finalize_assets(os)
        objs = sum(objs, start=[])
        # ★尺寸釘選也要管「新建撒點系統」★(2026-09-13 plain_lily：white_calla_lily ×0.75 沒生效 ——
        #   它走 extra create flowers(場景原本沒有這個系統)，母本在官方撒點模組自己的 apply() 裡建，
        #   原本只處理 hero proxy / restyle 母本 / placeholder populate 三條路)。
        #   縮頂點(跟 restyle 一樣；instance 不一定吃物件 scale)，同一個 mesh 只縮一次；
        #   這裡在 scatter_instances 用母本體積算密度之前 ⇒ 密度照 infinigen 公式自動重算。
        if scale and abs(float(scale) - 1.0) > 1e-6:
            # 縮頂點只對「幾何已經在 mesh 裡」的母本有效；花瓣若是 GN modifier 長出來的會縮不到(87 提醒)
            #   ⇒ 記下縮放前後 evaluated 尺寸(obj.dimensions 會算進 modifier)，實跑後一眼看得出有沒有 ×scale。
            def _dims(roots):
                bpy.context.view_layer.update()
                return [tuple(round(float(v), 3) for v in r.dimensions) for r in roots[:3]]
            dims_before = _dims(objs)
            seen_meshes = set()
            moved_children = 0
            for root in objs:
                # 多物件組成的母本：子物件相對父物件的位置也要一起縮，不然花頭會留在原高度、跟莖分開(87 提醒)
                for child in getattr(root, "children_recursive", ()):
                    child.location = child.location * float(scale)
                    moved_children += 1
                for ob in (root, *getattr(root, "children_recursive", ())):
                    data = getattr(ob, "data", None)
                    if getattr(ob, "type", None) != "MESH" or data is None:
                        continue
                    if data.as_pointer() in seen_meshes:
                        continue
                    seen_meshes.add(data.as_pointer())
                    for v in data.vertices:
                        v.co *= float(scale)
                    data.update()
            dims_after = _dims(objs)
            ratios = [round(a[i] / b[i], 3) for a, b in zip(dims_after, dims_before)
                      for i in range(3) if b[i] > 1e-6]
            report["asset_scale_pin"] = {"factor": float(scale), "meshes_scaled": len(seen_meshes),
                                         "children_moved": moved_children,
                                         "dims_before": dims_before, "dims_after": dims_after,
                                         "ratios": ratios}
            if not ratios:
                # root 是 Empty(幾何在子物件)時 dimensions 全 0 ⇒ 等於沒量到，不要讓空清單看起來像沒問題(87 提醒)
                print(f"[extra]   ⚠️ 新建撒點母本縮放後★量不到實際倍率★(root dimensions 是 0，可能是 Empty)；"
                      f"請看成品確認大小", flush=True)
            print(f"[extra]   📏 新建撒點母本照釘選 ×{float(scale)} 套到 {len(seen_meshes)} 個 mesh、"
                  f"{moved_children} 個子物件位置；"
                  f"evaluated 尺寸 {dims_before} → {dims_after}(實際倍率 {sorted(set(ratios))})", flush=True)
        if as_list:
            return objs
        col = butil.group_in_collection(objs, name=f"assets:{name}", reuse=False)
        col.hide_viewport = True
        col.hide_render = True
        return col

    return wrapper


def _overlay_declared_factory(code_dir) -> str:
    """讀 overlay 自己的 factory.txt，取出它宣告的類名。"""
    meta = {}
    for line in (Path(code_dir) / "factory.txt").read_text(
            encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    name = meta.get("factory", "")
    if not name:
        name = meta["fqn"].rsplit(".", 1)[-1]
    return name


def load_overlay_factory(code_dir, expect_name=None):
    """從 generator 產物載入改寫版 Factory ——★轉呼叫共用實作★。

    2026-09-08 修正。原本這裡有一份 8/10 寫的本地實作，用
    spec_from_file_location ★只載入 factory.txt 指到的那一個檔★。
    overlay 常常改寫★不只一個檔★(overlay_table 的 rewrote 就是清單)，
    其餘檔案會被靜默忽略、fallback 到官方版：

      sunflower_field 改寫 flowerplant.py + flower.py，
      factory.txt 只指 flowerplant.py ⇒ 它第 12 行的
        from infinigen.assets.objects.grassland import flower as Flower
      吃到官方 flower.py ⇒ 花瓣用官方 petal_hsv()(色相近乎均勻隨機)，
      而不是 overlay 寫的 plant_yellow() ⇒ ★向日葵渲成隨機紫青綠★。

    舊註解寫「不能用 sys.path.insert(2026-07-28 實測失敗)」——那個顧慮
    (上層 package 已被 import，快取的 __path__ 會拿到官方版)已由共用版的
    purge_infinigen_modules() 解掉，placer.py 從 9/1 起就走共用版。
    """
    if expect_name is None:
        expect_name = _overlay_declared_factory(code_dir)
    return _shared_load_overlay_factory(code_dir, expect_name)


# ════════════════════════════════════════════════════════ HERO
# 跨 edit 累積的投影失敗；hero 迴圈跑完才一次丟出，這樣 machine_review 拿到的
# failed_instances 會涵蓋★每一個★被拒的 edit，而不只是最先失敗的那一個。
pending_projection_failures = []
for idx, e in enumerate(hero_edits):
    op, fac = e.get("op"), e.get("factory")
    entry = {"index": idx, "op": op, "factory": fac,
             "reason_from_planner": e.get("reason"), "feasible": True}
    if op in HERO_ATTR_OPS:
        # ★不動 box★ —— 這一筆只是把 replace 的擺放屬性帶下來，由 plan_attrs 收走。
        tgt = e.get("target_factory") or e.get("target") or fac
        entry.update(op="attrs", target_factory=tgt,
                     anchoring=e.get("anchoring"), n_masters=e.get("n_masters"),
                     world_motion=e.get("world_motion"),
                     local_animation=e.get("local_animation"),
                     code_from=e.get("code_from"),
                     note="只帶擺放屬性，不動 box")
        if e.get("code_from"):
            collection_code_intent[tgt] = e["code_from"]
        print(f"[hero] ℹ️ #{idx} attrs {tgt}: anchoring={e.get('anchoring')!r} "
              f"n_masters={e.get('n_masters')!r}", flush=True)
        report["hero_results"].append(entry)
        continue
    if op not in HERO_OPS:
        entry.update(feasible=False,
                     blocked_reason=f"hero 不支援 op {op!r}"
                                    f"(可用 {sorted(HERO_OPS | HERO_ATTR_OPS)})")
    elif fac not in HERO_FACTORIES and op != "add":
        # ★op == "add" 不在此擋★(2026-07-29)：場景沒有的 factory 仍然可以「佔位」，
        #   由 add 分支借用代理 box。★階段 A 只決定位置，外觀是階段 C 的事。★
        #   (delete/scale/move 就必須擋 —— 它們要動的是★已經存在★的 box。)
        entry.update(feasible=False,
                     blocked_reason=f"{fac} 不是 HERO factory —— populate_all 不處理它。"
                                    f"若是 ground_scatter 請放進 extra_edits")
    elif op != "add" and not colls_for(fac):
        entry.update(feasible=False, blocked_reason=f"場景沒有 placeholders:{fac}(*)")

    if not entry["feasible"]:
        print(f"[hero] ⛔ #{idx} {op}:{fac} —— {entry['blocked_reason'][:70]}", flush=True)
        report["hero_results"].append(entry)
        continue
    report["needs_populate"] = True

    if op == "delete":
        mode, val = e.get("rule", "frac:0.5").split(":")
        val = float(val)
        tb = ta = td = tdi = 0
        for coll, _seed in colls_for(fac):
            roots = roots_of(coll)
            info = sorted(((o.location - campos).length, o) for o in roots)
            if mode == "keep":
                keep = {o.name for _d, o in info[:int(val)]}
            elif mode == "keep_inview":
                keep = set([o.name for _d, o in info if o.name in INVIEW][:int(val)])
            elif mode == "frac":
                keep = {o.name for o in random.sample(roots, max(0, int(round(len(roots) * val))))}
            else:
                entry.update(feasible=False, blocked_reason=f"不認得 rule {mode!r}")
                break
            victims = [o for o in roots if o.name not in keep]
            tdi += sum(1 for o in victims if o.name in INVIEW)
            tb += len(roots); td += len(victims); ta += len(roots) - len(victims)
            if not DRY_RUN:
                for o in victims:
                    for x in [o] + o.children_recursive:
                        try:
                            bpy.data.objects.remove(x, do_unlink=True)
                        except Exception:
                            pass
        entry.update(roots_before=tb, roots_after=ta, deleted=td, deleted_in_view=tdi,
                     note=f"刪掉 {td} 個裡只有 {tdi} 個是 populate 看得到的，"
                          f"其餘 {td - tdi} 個★本來就不會長出來，render 看不出差異★")
        print(f"[hero] ✅ #{idx} delete {fac}: {tb}→{ta} (in-view {tdi})", flush=True)

    elif op == "scale":
        v = e.get("value", 1.0)
        sx, sy, sz = (v, v, v) if isinstance(v, (int, float)) else v
        n = 0
        for coll, _s in colls_for(fac):
            for o in roots_of(coll):
                if not DRY_RUN:
                    o.scale = (o.scale.x * sx, o.scale.y * sy, o.scale.z * sz)
                n += 1
        entry.update(n=n, scale=[sx, sy, sz],
                     note="scale 是 parent 繼承(等比縮放同一棵樹，不是重長)；"
                          "且 cull 量盒子幾何 ⇒ ★縮小會讓部分 placeholder 掉出視野★")
        print(f"[hero] ✅ #{idx} scale {fac}: n={n} ×({sx},{sy},{sz})", flush=True)

    elif op == "move":
        dx, dy, dz = e.get("delta", [0, 0, 0])
        n = 0
        for coll, _s in colls_for(fac):
            for o in roots_of(coll):
                if not DRY_RUN:
                    o.location += Vector((dx, dy, dz))
                n += 1
        entry.update(n=n, delta=[dx, dy, dz])
        print(f"[hero] ✅ #{idx} move {fac}: n={n}", flush=True)

    elif op == "add":
        n_add, radius = int(e.get("n", 10)), float(e.get("radius", 10.0))
        # director 的 edit_plan 帶下來的；沒有就 None ⇒ 維持原本 (0,0,random) 行為
        anchoring = e.get("anchoring") if e.get("anchoring") in pm.ANCHORINGS else None
        world_motion = wm.validate_execution_context(
            e.get("world_motion"), action="add", anchoring=anchoring)
        legacy_recovery = plc.recover_legacy_outdoor_ultra_loose_edit(e)
        if legacy_recovery is not None:
            print(
                f"[hero] restored legacy outdoor ultra-loose for "
                f"{e.get('code_from') or fac}: "
                f"{legacy_recovery.get('restored_fields')}",
                flush=True)
        raw_placement_spec = (
            e.get("placement_spec")
            if isinstance(e.get("placement_spec"), dict) else {})
        dense_count = plc.dense_field_instance_count(
            count=n_add,
            factory=fac,
            code_from=e.get("code_from") or e.get("id"),
            semantic_target=str(
                raw_placement_spec.get("semantic_target") or "").strip(),
            arrangement=str(raw_placement_spec.get("arrangement") or ""),
        )
        if dense_count != n_add:
            print(f"[hero] [field] {e.get('id')}: dense field count {n_add} -> "
                  f"{dense_count}", flush=True)
            n_add = dense_count
            requested_screen_positions = None
            requested_placement_yaws = None
        placement_spec = plc.normalize_placement_spec(
            raw_placement_spec, count=n_add, anchoring=anchoring,
            factory=fac, code_from=e.get("code_from"))
        if (uses_surface_map(anchoring)
                and requires_full_footprint_structure_support(
                    factory=fac,
                    code_from=e.get("code_from"),
                    semantic_target=placement_spec.get("semantic_target"))):
            placement_spec["surface_policy"][
                "require_full_footprint_support"] = True
            e.setdefault("placement_spec", {})["surface_policy"] = deepcopy(
                placement_spec["surface_policy"])
        declared_proxy_dimensions = placement_spec.get("proxy_dimensions_m")
        inferred_proxy_dimensions = (
            None if declared_proxy_dimensions is not None else
            plc.inferred_proxy_dimensions(
                factory=fac,
                code_from=e.get("code_from"),
                anchoring=anchoring,
                semantic_target=placement_spec.get("semantic_target"),
            ))
        proxy_dimensions = declared_proxy_dimensions or inferred_proxy_dimensions
        proxy_dimension_source = (
            "director_estimate" if declared_proxy_dimensions is not None else
            plc.inferred_proxy_dimension_source(
                factory=fac,
                code_from=e.get("code_from"),
                anchoring=anchoring,
                semantic_target=placement_spec.get("semantic_target"),
            ) if inferred_proxy_dimensions is not None else None)
        min_sep_value = e.get("min_sep")
        if min_sep_value is None:
            min_sep_value = plc.default_min_sep(
                factory=fac,
                code_from=e.get("code_from"),
                anchoring=anchoring,
                semantic_target=placement_spec.get("semantic_target"),
                arrangement=placement_spec.get("arrangement"),
                count=n_add,
            )
        min_sep = float(min_sep_value)
        requested_screen_positions = e.get("screen_positions")
        requested_placement_yaws = e.get("placement_yaws")
        if requested_screen_positions is None:
            requested_screen_positions = plc.initial_screen_positions(
                placement_spec, n_add)
        if requested_screen_positions is not None:
            requested_screen_positions = plc.normalize_screen_positions(
                requested_screen_positions, spec=placement_spec, count=n_add)
        preview_labels = e.get("placement_preview_labels")
        if preview_labels is not None:
            if (not isinstance(preview_labels, list)
                    or len(preview_labels) != n_add
                    or any(not isinstance(label, str) or not label.strip()
                           for label in preview_labels)):
                raise ValueError(
                    "placement_preview_labels must contain one non-empty string "
                    f"per instance (expected {n_add})")
        entry["placement_spec"] = placement_spec
        if anchoring:
            nf, alt = pm.params_for(anchoring)
            print(f"[hero] anchoring={anchoring} (normal_fac={nf}, altitude={alt:+.2f})", flush=True)
        colls = colls_for(fac)
        requested_code_from = str(e.get("code_from") or "")
        # Adds that share a factory but point at different generated codes must
        # not collapse into one placeholder collection.  Once a collection is
        # claimed by an earlier add in this plan, reserve it for that edit and
        # force the later add to get its own collection identity.
        if requested_code_from:
            colls = [
                (coll, seed) for coll, seed in colls
                if collection_code_intent.get(coll.name) in (None, requested_code_from)
            ]
        placeholder_only = False
        if not colls:
            # ★★場景沒有這個 factory ⇒ 建「自己的 collection」佔位★★
            #   (2026-07-29；★取代先前「借用別人 collection」的錯誤做法★)
            #
            # ⚠️ ★為什麼不能塞進代理的 collection★(第一版寫錯，實測抓到)：
            #    階段 C 是 ★per-collection★ 的 —— 整個 collection 只能用一個 Factory 生成。
            #    先前把 120 個燈籠 box 塞進 placeholders:DragonflyFactory(...)，
            #    結果那個 collection 變成 122 個(2 蜻蜓 + 120 燈籠)，
            #    ⇒ ★要嘛蜻蜓被誤換成燈籠，要嘛燈籠全變蜻蜓，必錯。★
            #
            # ★正確做法★：新建 placeholders:<目標factory>(<seed>)，原有 collection 完全不動。
            #    populate_all 用 ★collection 名的 regex 解析 factory 名★(placement.py:288)，
            #    所以 collection ★必須★叫目標 factory，階段 C 才找得到它。
            #    seed 只用來 factory_class(seed) 選品種變體 ⇒ 全新 factory 給任何值都可以。
            #
            # ★★不借用任何既有 factory —— 自己建中性的 box★★
            #   (2026-07-29；★推翻先前「必須借骨架」的假設★)
            #
            # ⚠️ ★我先前錯在哪★：以為 create_asset 一定要 placeholder.children[0]
            #    (0728 6-D 的 IndexError)，所以非得借一個既有 factory 的骨架不可。
            #    ★但那是「樹/灌木」專屬的需求★ —— 它們覆寫了 create_placeholder
            #    去附加骨架，create_asset 才會去讀 children[0]。
            #
            # ★Infinigen 的預設(factory.py:44-46)★：
            #      def create_placeholder(self, **kwargs):
            #          return butil.spawn_cube(size=2)      ← ★就是一個普通立方體★
            #    ⇒ 全新的 Factory(機器人/燈籠)用預設的就好，
            #      ★create_asset 自己建幾何，根本不會去讀 children★。
            #    ⇒ 借骨架反而有害：借來的是「灌木的枝幹結構」，語意不相容，
            #      而且 --real-size 會依骨架尺寸畫 box ⇒ ★預覽圖嚴重誤導★。
            new_seed = random.randint(1, 99999999)
            new_name = f"placeholders:{fac}({new_seed})"
            if not DRY_RUN:
                new_coll = bpy.data.collections.new(new_name)
                assert new_coll.name == new_name, \
                    f"Blender 把 {new_name} 截斷成 {new_coll.name}"
                bpy.context.scene.collection.children.link(new_coll)
                colls = [(new_coll, new_seed)]
            else:
                colls = [(None, new_seed)]       # dry-run 不建 collection
            placeholder_only = True
            entry.update(new_collection=new_name, box_source="neutral_cube")
            entry["proxy_dimensions_m"] = proxy_dimensions or [2.0, 2.0, 2.0]
            entry["proxy_dimension_source"] = (
                proxy_dimension_source or "legacy_2m_cube_fallback")
            entry["note"] = (f"★場景沒有 {fac} ⇒ 新建 {new_name}★，"
                             f"box 用 ★中性立方體★(Infinigen 預設 spawn_cube)，"
                             f"★不借用任何既有 factory★。外觀由階段 C 的 code 決定。")
            print(f"[hero] ⚠️ 場景沒有 {fac} → ★新建 {new_name}(中性 box)★", flush=True)
        root_sets = [roots_of(candidate) if candidate is not None else []
                     for candidate, _seed in colls]
        template_choice = plc.placeholder_template_choice(
            [len(candidate_roots) for candidate_roots in root_sets])
        collection_index = template_choice["collection_index"] or 0
        coll, seed = colls[collection_index]
        roots = root_sets[collection_index]
        if template_choice["use_neutral_proxy"] and not placeholder_only:
            # An empty collection is a valid Stage-C routing identity but not a
            # template source.  The 0818 Coast diagnostic had an empty
            # FlyingBirdFactory collection and crashed on k % len(templates).
            # Reuse the collection name/seed and create the same neutral proxy
            # used for a genuinely new Factory.
            placeholder_only = True
            entry.update(
                empty_existing_collection=coll.name if coll is not None else None,
                box_source="neutral_cube",
                proxy_dimensions_m=proxy_dimensions or [2.0, 2.0, 2.0],
                proxy_dimension_source=(
                    proxy_dimension_source or "legacy_2m_cube_fallback"),
                note=(f"★既有 {coll.name if coll is not None else fac} collection 為空★，"
                      "改建中性 proxy；保留原 collection identity 給 Stage C。"),
            )
            print(
                f"[hero] ⚠️ {coll.name if coll is not None else fac} 沒有 template "
                "→ 在原 collection 建中性 box",
                flush=True,
            )
        # 場景本來就有這個 factory 時，複製它既有的 box(連骨架)；
        # ★全新 factory 則不需要範本★ —— 直接建中性立方體。
        templates = roots[:min(len(roots), 12)] if not placeholder_only else []
        tpl_roots = roots
        max_i = max((int(m.group(1)) for o in roots
                     if (m := re.search(r"spawn_placeholder\((\d+)\)", o.name))), default=-1)
        deps = bpy.context.evaluated_depsgraph_get()
        # ★2026-08-09 新增兩個 plan 欄位★
        #   "positions": [[x,y,z], ...]  —— ★完全指定落點★，跳過取樣。
        #       用途：取樣依賴 ray_cast 打到 OpaqueTerrain，但★有些場景根本沒有★
        #       (arctic 只有 CollectiveTransparentTerrain ⇒ 射線必然全空、物件飄空中)。
        #       這種場景與「就是要放在這一塊」的鏡頭語言，都只能靠明確座標。
        #   "min_sep": float —— 落點最小間距。預設 2.5 是常數、不看資產實際尺寸
        #       (0809 十三節的已知問題：開屏孔雀寬 4.8m 卻用 1.25m 間距 ⇒ 互相遮擋)。
        explicit = e.get("positions")
        if explicit:
            # ★盡量模式會在 positions 留下 None 洞★(2026-09-12)：
            #   placement_region_contract 先填 [None]*n 再★逐格★寫入，塞不下的
            #   instance 就一直是 None，而 Vector(None) 直接 TypeError ⇒ 整個 case 死。
            #   (arctic 10 隻企鵝 9 格：0911 死在「capacity 不足」，我改成盡量模式後
            #    改死在這裡 —— 同一個洞換個地方掉下去。)
            #   丟掉沒拿到格子的，交給下面既有的「n 收斂」邏輯 —— 那正是盡量模式的
            #   本意：放不下就少放幾個，不要整個 case 失敗。
            #   ⚠️ explicit_position_supports 是★按 index 對齊★的(下面 supports[k])，
            #      必須同步過濾，否則屬性會貼到別隻身上。
            _asked = explicit[:n_add]
            _supports = e.get("explicit_position_supports") or []
            _kept = [(point, _supports[i] if i < len(_supports) else None)
                     for i, point in enumerate(_asked) if point is not None]
            if len(_kept) < len(_asked):
                print(f"[hero] ⚠️ {len(_asked) - len(_kept)}/{len(_asked)} 個 instance "
                      f"沒拿到格子(盡量模式) ⇒ 丟掉", flush=True)
                e["explicit_position_supports"] = [row for _, row in _kept]
            positions = [Vector(point) for point, _ in _kept]
            if len(positions) < n_add:
                print(f"[hero] ⚠️ positions 只給了 {len(positions)} 個但 n={n_add} "
                      f"⇒ ★n 收斂成 {len(positions)}★", flush=True)
                n_add = len(positions)
            in_view_n = sum(1 for p in positions if _in_camera_view(p))
            print(f"[hero] ★落點由 plan 明確指定★ {n_add} 個(不取樣) "
                  f"畫面內 {in_view_n}/{n_add}", flush=True)
            entry["positions_source"] = "explicit"
            explicit_supports = e.get("explicit_position_supports") or []
            support_surfaces = []
            support_normals = []
            for support_index in range(n_add):
                support = (
                    explicit_supports[support_index]
                    if support_index < len(explicit_supports)
                    and isinstance(explicit_supports[support_index], dict)
                    else {})
                support_surfaces.append(
                    str(support.get("surface") or support.get("hit_surface"))
                    if (support.get("surface") or support.get("hit_surface"))
                    else None)
                normal_values = (
                    support.get("surface_normal")
                    or support.get("normal"))
                support_normals.append(
                    Vector(normal_values)
                    if normal_values is not None else None)
        else:
            if templates:
                local_points = [Vector(corner) for corner in templates[0].bound_box]
                half_x = max(abs(point.x) for point in local_points) * abs(templates[0].scale.x)
                half_y = max(abs(point.y) for point in local_points) * abs(templates[0].scale.y)
                half_z = max(abs(point.z) for point in local_points) * abs(templates[0].scale.z)
                footprint_half_extents = (max(half_x, 0.1), max(half_y, 0.1))
                box_half_height = max(half_z, 0.1)
            else:
                # A genuinely new factory has no geometry yet.  Size its
                # neutral box from the Director's conservative estimate rather
                # than pretending every future asset is a 2m cube.
                footprint_half_extents = (
                    tuple(float(value) / 2.0 for value in proxy_dimensions[:2])
                    if proxy_dimensions is not None else (1.0, 1.0))
                box_half_height = (
                    float(proxy_dimensions[2]) / 2.0
                    if proxy_dimensions is not None else 1.0)
            try:
                positions, in_view_n, support_surfaces, support_normals = sample_add_positions(
                    n_add, radius, roots or tpl_roots,
                    min_sep=min_sep,
                    screen_positions=requested_screen_positions,
                    screen_bounds=placement_spec["screen_bounds"],
                screen_yaws=requested_placement_yaws,
                screen_projection=e.get("screen_position_projection", "terrain"),
                allow_projection_misses=bool(
                    e.get("candidate_preview_allow_projection_misses")
                    or e.get("diagnostic_preview_allow_rejected_projection")),
                surface_policy=placement_spec["surface_policy"],
                    footprint_half_extents=footprint_half_extents,
                    box_half_height=box_half_height)
            except plc.PlacementProjectionFailure as error:
                # ★不要就地 raise★ —— 那會中斷 hero 迴圈，讓後面的 edit 連檢查都
                #   沒被檢查過，machine_review 只看得到「第一個」失敗的 edit，
                #   composer 也就永遠拿不到修正後面那些 edit 的授權。
                #   (0824 arctic：penguins 先失敗 ⇒ igloo 整整三輪都沒被回報，
                #    冰屋就一直飄在 liquid 上。)
                #   改成收集起來，跑完所有 hero_edits 再一次全部丟出。
                for failure in error.failures:
                    failure["edit_id"] = str(e.get("code_from") or fac)
                pending_projection_failures.extend(error.failures)
                entry.update(feasible=False,
                             blocked_reason="projection_rejected")
                report["hero_results"].append(entry)
                print(f"[hero] ⛔ #{idx} {op}:{fac} —— 投影被拒 "
                      f"{len(error.failures)} 個 instance(續查其餘 edit)", flush=True)
                continue
            if requested_screen_positions:
                entry["positions_source"] = "screen_positions"
                entry["screen_position_projection"] = e.get(
                    "screen_position_projection", "terrain")
        entry["in_view"] = f"{in_view_n}/{n_add}"
        shared_yaw = None
        if placement_spec["orientation"] == "parallel":
            shared_yaw = plc.parallel_row_yaw(positions)
            if shared_yaw is None:
                shared_yaw = random.uniform(0, math.tau)
        for k in range(n_add):
            pos = positions[k]
            instance_index = max_i + 1 + k
            full_nm = f"{fac}({seed}).spawn_placeholder({instance_index})"
            nm = plc.blender_safe_placeholder_name(fac, seed, instance_index)
            added_names.append(nm)
            if placeholder_only:
                proxy_intent[nm] = fac
            if not DRY_RUN:
                if placeholder_only:
                    # ★全新 factory：建中性 proxy box★。
                    #   ⚠️ ★不複製 children★ —— 沒有骨架是★正確的★：
                    #     骨架是樹/灌木專屬(它們覆寫 create_placeholder 才有)，
                    #     全新 factory 的 create_asset 自己建幾何，不會讀 children。
                    #   ⚠️ 尺寸必須烤進 mesh，不能寫在 object.scale。populate 會把
                    #     真 asset parent 到 placeholder；三軸 scale 會把模型永久拉扁。
                    #     mesh 是長方體、transform 仍為單位，才能同時得到準確 proxy
                    #     與不變形的生成資產。
                    me = bpy.data.meshes.new(nm)
                    import bmesh as _bm
                    _b = _bm.new()
                    _bm.ops.create_cube(_b, size=2.0)
                    if proxy_dimensions is not None:
                        _bm.ops.scale(
                            _b,
                            vec=Vector(tuple(
                                float(value) / 2.0
                                for value in proxy_dimensions)),
                            verts=_b.verts,
                        )
                    _b.to_mesh(me)
                    _b.free()
                    o = bpy.data.objects.new(nm, me)
                    o.location = pos
                    if proxy_dimensions is not None:
                        o["bi_proxy_dimensions_m"] = json.dumps(
                            proxy_dimensions)
                        o["bi_proxy_dimension_source"] = (
                            proxy_dimension_source or "director_estimate")
                    else:
                        o["bi_proxy_dimension_source"] = "legacy_2m_cube_fallback"
                    coll.objects.link(o)
                    # ★階段 A 不做貼地旋轉，只傳屬性★(使用者 2026-08-04 定的分工)
                    #   box 照 infinigen 的慣例只繞 Z(Euler(0,0,yaw))，貼地旋轉與落地
                    #   ★一律留到 populate 之後★由 placer 用真幾何做。
                    #   ⇒ 理由：在這層只有 2m 立方體/被取代物的 box，bbox 不等於資產的
                    #     bbox，沉降一定要重算一次；既然如此在這裡轉就是多餘的一步。
                    yaw = (requested_placement_yaws[k]
                           if requested_placement_yaws is not None
                           else shared_yaw if shared_yaw is not None
                           else random.uniform(0, math.tau))
                    o.rotation_euler = (0, 0, yaw)
                    stamp_anchor(o, anchoring, yaw)
                else:
                    # ``placeholder_only`` also covers an existing-but-empty
                    # collection, so this branch always has at least one template.
                    tpl = templates[k % len(templates)]
                    o = tpl.copy()
                    if tpl.data:
                        o.data = tpl.data.copy()
                    o.name = nm
                    # ★這條路也要蓋 bi_proxy_dimensions_m★(2026-09-12)：placer 的
                    #   proxy-fit 迴圈只看有蓋章的 anchor。0912 plain 的 8 隻蜜蜂被加進
                    #   baseline 原有的 DragonflyFactory(4176809)，走的是這條複製模板的
                    #   路 ⇒ 沒蓋章 ⇒ 沒被縮到 0.3m ⇒ 停在原生 ~1.1m。canyon 的蝴蝶是
                    #   全新 collection(上面那條)有蓋章 ⇒ 縮到 0.16m。同一個 factory
                    #   兩種尺寸，純看場景原本有沒有這種 factory —— 不能這樣。
                    if proxy_dimensions is not None:
                        o["bi_proxy_dimensions_m"] = json.dumps(proxy_dimensions)
                        o["bi_proxy_dimension_source"] = (
                            proxy_dimension_source or "director_estimate")
                    # A copied placeholder may carry an old path action and a
                    # FOLLOW_PATH constraint (FlyingBirdFactory does).  Those
                    # immediately override the newly sampled location on the next
                    # depsgraph evaluation and put the requested asset back on the
                    # donor bird's unrelated trajectory.  Stage A owns the new
                    # position; requested world motion is authored later by placer.
                    o.animation_data_clear()
                    for constraint in list(o.constraints):
                        o.constraints.remove(constraint)
                    o.location = pos
                    coll.objects.link(o)
                    yaw = (requested_placement_yaws[k]
                           if requested_placement_yaws is not None
                           else shared_yaw if shared_yaw is not None
                           else random.uniform(0, math.tau))
                    # Stage A owns only the placeholder position.  Stage C settles
                    # the real populated geometry exactly once.  Calling place()
                    # here used to add airborne/submerged altitude once now and a
                    # second time in placer (+6m for an anchoring altitude of +3m).
                    o.rotation_euler = (0, 0, yaw)
                    stamp_anchor(o, anchoring, yaw)
                    # ★★複製 children★★ child 是骨架(generate.py:142)，缺了會 IndexError
                    #   ⚠️ 只有「複製既有 factory」時才需要 —— 那批本來就有骨架。
                    for ch in tpl.children:
                        c2 = ch.copy()
                        if ch.data:
                            c2.data = ch.data.copy()
                        coll.objects.link(c2)
                        c2.parent = o
                        c2.matrix_parent_inverse = ch.matrix_parent_inverse.copy()
                if e.get("code_from"):
                    o["bi_code_from"] = str(e["code_from"])
                if nm != full_nm:
                    o["bi_full_placeholder_name"] = full_nm
                o["bi_instance_index"] = int(k)
                if requested_screen_positions is not None:
                    o["bi_screen_x"] = float(requested_screen_positions[k][0])
                    o["bi_screen_y"] = float(requested_screen_positions[k][1])
                if preview_labels is not None:
                    o["bi_preview_label"] = preview_labels[k].strip()
                if support_surfaces[k]:
                    o["bi_support_surface"] = str(support_surfaces[k])
                if support_normals[k] is not None:
                    o["bi_support_normal"] = [
                        float(value) for value in support_normals[k]]
                    o["bi_support_slope_deg"] = float(math.degrees(math.acos(
                        max(0.0, min(1.0, abs(support_normals[k].z))))))
                if explicit:
                    supports = e.get("explicit_position_supports") or []
                    support = (
                        supports[k] if k < len(supports)
                        and isinstance(supports[k], dict) else {})
                    for support_key, prop_name in (
                            ("semantic_cell_id", "bi_semantic_cell_id"),
                            ("cell_id", "bi_semantic_grid_cell_id"),
                            ("region_id", "bi_semantic_region_id"),
                            ("support_plane_id", "bi_semantic_support_plane_id")):
                        if support.get(support_key):
                            o[prop_name] = str(support[support_key])
                o["bi_placement_arrangement"] = placement_spec["arrangement"]
                o["bi_placement_semantic_target"] = placement_spec["semantic_target"]
                o["bi_placement_screen_region"] = placement_spec["screen_region"]
                o["bi_placement_orientation"] = placement_spec["orientation"]
                o["bi_surface_policy"] = json.dumps(
                    placement_spec["surface_policy"], sort_keys=True)
                if (set(placement_spec["surface_policy"].get(
                        "required_surface_types") or [])
                        & plc.INDOOR_PLANAR_SURFACE_TYPES):
                    # ``upright`` normally embeds outdoor posts by 10 cm.
                    # Furniture-supported indoor objects must rest on top of
                    # the selected plane instead of inheriting that embed.
                    o["bi_support_contact_offset_m"] = 0.0
                o["bi_world_motion_preset"] = world_motion["preset"]
                o["bi_world_motion_intensity"] = world_motion["intensity"]
                o["bi_world_motion_params"] = json.dumps(
                    world_motion["params"], sort_keys=True)
        entry.update(added=n_add, index_from=max_i + 1,
                     collection=coll.name if coll is not None else new_name,
                     world_motion=world_motion)
        if e.get("code_from"):
            collection_code_intent[entry["collection"]] = e["code_from"]
            entry["code_from"] = e["code_from"]
        # ★把「幾個真的在畫面內」印出來★ —— 這件事以前完全沒有人量，
        #   結果 e2e_peacock_candy 的孔雀 4/4 在畫面外，卻一路綠燈到最後一張圖。
        _mark = "✅" if in_view_n == n_add else ("⚠️" if in_view_n else "⛔")
        print(f"[hero] ✅ #{idx} add {fac}: +{n_add}  "
              f"{_mark} ★畫面內 {in_view_n}/{n_add}★", flush=True)

    report["hero_results"].append(entry)

# 所有 hero_edits 都檢查過了才回報，讓 composer 一次看到全部被拒的 instance。
# 跟以前一樣走例外路徑 ⇒ 不寫 out_blend、不寫 report。
if pending_projection_failures:
    raise plc.PlacementProjectionFailure(pending_projection_failures)


# ════════════════════════════════════════════════════════ EXTRA
def scatter_semantic_id(obj):
    """Resolve stable metadata first, then the anchored native migration map."""
    explicit = sc.canonical_semantic_id(obj.get(sc.SEMANTIC_ID_PROPERTY))
    return explicit or sc.semantic_id_from_carrier_name(obj.name)


def find_scatter(target):
    semantic_id = sc.canonical_semantic_id(target)
    if semantic_id is None:
        return []
    return [
        obj for obj in bpy.data.objects
        if obj.name.startswith("scatter:")
        and scatter_semantic_id(obj) == semantic_id
    ]


def stamp_scatter_carriers(objects, semantic_id, adapter_pass):
    stamped = []
    for obj in objects:
        if obj is None or not obj.name.startswith("scatter:"):
            continue
        obj[sc.SEMANTIC_ID_PROPERTY] = semantic_id
        obj["bi_scatter_adapter_pass"] = adapter_pass
        stamped.append(obj)
    return stamped


def remove_scatter_carriers(objects):
    names = [obj.name for obj in objects]
    for obj in list(objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    return names


def gn_of(o):
    return next((m for m in o.modifiers if m.type == "NODES"), None)


def density_socket(md):
    for item in md.node_group.interface.items_tree:
        if getattr(item, "in_out", "") == "INPUT" and item.name == "Overall Density":
            return item.identifier
    return None


def master_input(md):
    for n in md.node_group.nodes:
        if n.type == "COLLECTION_INFO":
            for i in n.inputs:
                if i.type == "COLLECTION":
                    return i
    return None


# ★官方只有 flowerplant/grass/fern/monocots/decorative_plants 這五個 scatter 會隨風搖★
#   (infinigen/assets/scatters/utils/wind.py)。珊瑚/海草/水母的 scatter 沒有，使用者想要
#   它們也有 idle 擺動 ⇒ 換母本時把同一條 wind 節點鏈接到既有載體的 Instance on Points。
WIND_SCATTER_SYSTEMS = {"coral_reef", "seaweed", "kelp", "jellyfish", "mollusk"}


def add_wind_to_carrier(md, *, speed=0.35, strength=12.0) -> bool:
    """把官方的 wind_rotation 接到既有 scatter 載體的實例旋轉上。成功回傳 True。"""
    from infinigen.assets.scatters.utils.wind import wind_rotation
    from infinigen.core.nodes.node_wrangler import NodeWrangler
    from infinigen.core.nodes.node_info import Nodes
    group = getattr(md, "node_group", None)
    if group is None:
        return False
    if any(n.bl_idname == "GeometryNodeInputSceneTime" for n in group.nodes):
        return False                      # 已經有時間驅動，不重複加
    points = next((n for n in group.nodes
                   if n.bl_idname == "GeometryNodeInstanceOnPoints"), None)
    if points is None or "Rotation" not in points.inputs:
        return False
    nw = NodeWrangler(group)
    swing = wind_rotation(nw, speed=speed, strength=strength)
    socket = points.inputs["Rotation"]
    if socket.is_linked:
        existing = socket.links[0].from_socket
        combined = nw.new_node(Nodes.RotateEuler,
                               input_kwargs={"Rotation": existing, "Rotate By": swing})
        group.links.new(combined.outputs[0], socket)
    else:
        group.links.new(swing.outputs[0], socket)
    return True


def restyle_scatter(scats, code_dir, n_masters_hint, entry, label, code_from=None):
    """把這個 scatter 系統的母本集合換成 overlay 生的 —— 位置/密度/分佈完全不動。

    ⚠️ ★所有 carrier 都要改★：一個語意系統可能有多個載體(coral_reef 實測是
       coral + coral.001)，只改第一個的話畫面上會一半新一半舊。
    """
    from infinigen.core.placement.factory import make_asset_collection
    first_md = gn_of(scats[0])
    first_inp = master_input(first_md)
    old_col = first_inp.default_value if first_inp else None
    n_masters = int(n_masters_hint or (len(old_col.objects) if old_col else 5))
    entry.update(old_master_collection=old_col.name if old_col else None,
                 n_masters=n_masters, code_dir=str(code_dir),
                 scatter_carriers=[obj.name for obj in scats])
    if DRY_RUN:
        return None
    FacClass, fqn = load_overlay_factory(code_dir)
    entry["fqn"] = fqn
    # ★母本要有顏色變化★(2026-09-13 使用者：珊瑚一片藍)：原本 n 個母本全用
    #   FacClass(0) ⇒ 調色盤在 __init__ 就依 factory_seed 定案 ⇒ n 個母本同一個色。
    #   make_asset_collection 吃 spawn_fns 清單，給它 n 個不同 factory_seed 的實例。
    new_col = make_asset_collection([FacClass(i) for i in range(max(1, n_masters))],
                                    n=n_masters, verbose=False,
                                    name=f"GENERATED_{label}")
    # ★換母本要重算密度★(2026-09-12 mountain：矮草 20cm 的撒點密度配 2m 芒草 = 一面牆)。
    #   照 infinigen 自己的公式(instance_scatter.scatter_instances)：
    #       density = vol_density / (avg_vol * avg_scale²)，avg_vol = mean(prod(dimensions))
    #   載體上的密度是建立時對舊母本算好的，換母本後 infinigen 不會重算 ⇒ 這裡補乘 舊體積/新體積。
    def _avg_volume(col):
        vols = [float(o.dimensions.x) * float(o.dimensions.y) * float(o.dimensions.z)
                for o in col.objects if o.type == "MESH" and max(o.dimensions) > 1e-6]
        return (sum(vols) / len(vols)) if vols else None
    # ★載體對舊母本設計的隨機放大(草叢 ×1~3)不該再套在新母本上★：讀載體 GN 裡的縮放常數與
    #   隨機下限算平均倍率，新母本頂點先除掉，讓它維持資產本身的尺寸。
    def _carrier_mean_scale(md):
        const, rand_min = None, None
        for node in md.node_group.nodes:
            if node.type == "VECT_MATH" and node.operation == "MULTIPLY":
                for sock_ in node.inputs:
                    if not sock_.is_linked and sock_.type == "VECTOR":
                        v = tuple(sock_.default_value)
                        if v[0] == v[1] == v[2] and v[0] > 1.0:
                            const = float(v[0])
            if node.type == "RANDOM_VALUE" and node.data_type == "FLOAT":
                lo, hi = float(node.inputs[2].default_value), float(node.inputs[3].default_value)
                if abs(hi - 1.0) < 1e-6 and lo < 1.0:
                    rand_min = lo
        return (const or 1.0) * (((rand_min if rand_min is not None else 1.0) + 1.0) / 2.0)
    mean_scale = _carrier_mean_scale(first_md)
    if mean_scale > 1.2:
        for ob in new_col.objects:
            if ob.type == "MESH":
                for v in ob.data.vertices:
                    v.co /= mean_scale
                ob.data.update()
        print(f"[extra]   載體平均放大 ×{mean_scale:.2f} ⇒ 新母本先除回原尺寸", flush=True)
    master_scale = asset_scale_for(code_from, entry.get("code_from"), label)
    if master_scale != 1.0:
        for ob in new_col.objects:
            if ob.type == "MESH":
                for v in ob.data.vertices:
                    v.co *= master_scale
                ob.data.update()
        print(f"[extra]   📏 新母本照釘選 ×{master_scale}(密度下面會照體積重算)", flush=True)
    old_vol, new_vol = _avg_volume(old_col) if old_col else None, _avg_volume(new_col)
    density_factor = 1.0
    if old_vol and new_vol and abs(new_vol - old_vol) > old_vol * 0.2:
        density_factor = old_vol / new_vol          # infinigen：density ∝ 1/母本體積
    changed = []
    for obj in scats:
        md = gn_of(obj)
        inp = master_input(md)
        if inp is None:
            continue
        inp.default_value = new_col
        sock = density_socket(md)
        if sock is not None and density_factor != 1.0:
            try:
                md[sock] = float(md[sock]) * density_factor
            except (KeyError, TypeError, ValueError):
                pass
        changed.append(obj.name)
    if density_factor != 1.0:
        print(f"[extra]   母本體積 {old_vol:.4f}→{new_vol:.4f}m³ ⇒ 撒點密度 ×{density_factor:.4f}(infinigen 公式)", flush=True)
    if str(label or "").lower() in WIND_SCATTER_SYSTEMS:
        swayed = []
        for obj in scats:
            try:
                if add_wind_to_carrier(gn_of(obj)):
                    swayed.append(obj.name)
            except Exception as error:      # 加不上就算了，不要弄壞這個 case
                print(f"[extra]   ⚠️ {obj.name} 加不了 idle 擺動: {error}", flush=True)
        if swayed:
            entry["wind_carriers"] = swayed
            print(f"[extra]   🌊 {len(swayed)} 個載體加上 idle 擺動(官方 wind)", flush=True)
    entry.update(new_master_collection=new_col.name,
                 new_masters_built=len(new_col.objects),
                 carriers_restyled=changed,
                 master_volume_m3={"old": old_vol, "new": new_vol},
                 density_factor=density_factor, carrier_mean_scale=mean_scale)
    return new_col


for idx, e in enumerate(extra_edits):
    op, target = e.get("op"), e.get("target")
    entry = {"index": idx, "op": op, "target": target,
             "reason_from_planner": e.get("reason"), "feasible": True}

    if op in EXTRA_IMPOSSIBLE:
        entry.update(feasible=False, blocked_reason=EXTRA_IMPOSSIBLE[op])
        print(f"[extra] ⛔ #{idx} {op}:{target} —— {EXTRA_IMPOSSIBLE[op][:60]}", flush=True)
        report["extra_results"].append(entry)
        continue
    if op not in EXTRA_OPS:
        entry.update(feasible=False,
                     blocked_reason=f"extra 不支援 op {op!r}(可用 {sorted(EXTRA_OPS)})")
        report["extra_results"].append(entry)
        continue

    # ── create：憑空長出一整個 scatter 系統(原本沒有的東西) ──
    #   ★2026-09-07 通用化★：原本寫死只能建 mushroom，現在整份走 scatter_catalog。
    #   三個新東西：
    #     selection  撒在哪 —— density.placement_mask(tag=domain) ，這是「撒在
    #                土壤/草地/水下」的唯一實作。★外疏內密是 mask 的雜訊本性，
    #                不用自己寫★。
    #     外觀替換   26 支 scatter 都在 apply() 內部用★模組全域名★查 factory，
    #                所以 setattr(module, symbol, cls) 一體適用；★不能沿用舊的
    #                inst.col = ... 寫法★，只有 5 支有 self.col。
    #     tag_dict   placement_mask(tag=...) 需要 density.set_tag_dict()，否則
    #                tag_dict 是 None 直接炸。來源是 coarse 帶過來的 MaskTag.json。
    if op == "create":
        existing_scatter = find_scatter(target)
        force_requested = bool(e.get("regression_force_create"))
        regression_mode = os.environ.get("BI_SCATTER_REGRESSION") == "1"
        entry["semantic_scatter_id"] = sc.canonical_semantic_id(target)
        if target not in CREATABLE_EXTRA:
            entry.update(feasible=False,
                         blocked_reason=f"不知道怎麼建 {target!r}(已知: {sorted(CREATABLE_EXTRA)})")
        elif force_requested and not regression_mode:
            entry.update(
                feasible=False,
                blocked_reason=(
                    "regression_force_create 只能在 BI_SCATTER_REGRESSION=1 使用"
                ),
            )
        elif existing_scatter and not force_requested:
            # ★場景已經有這個系統 ⇒ 換它的母本，不是拒絕★(2026-09-11)
            #   先前直接判 feasible=False「用 density 調整即可」，於是資產★整個沒進場景★
            #   而 case 照樣 PASS(0911 cave 的藍蘑菇、coral_reef 的花珊瑚叢都這樣消失)。
            #   「把地面鋪滿藍蘑菇」跟「把既有蘑菇換成藍蘑菇」在畫面上是同一件事。
            code_dir = Path(str(e.get("code") or ""))
            if not (code_dir / "factory.txt").is_file():
                entry.update(
                    feasible=False,
                    blocked_reason=(
                        f"場景已有 semantic scatter id={target!r}，但這筆沒有可用的 "
                        f"overlay(code={e.get('code')!r})，換不了外觀"
                    ),
                )
            else:
                entry["restyled_existing_scatter"] = True
                restyle_scatter(existing_scatter, code_dir, e.get("n_masters"),
                                entry, target, code_from=e.get("code_from"))
                print(f"[extra] ✅ #{idx} create {target}: 場景已有這個系統 ⇒ "
                      f"改成換母本 {entry.get('old_master_collection')} → "
                      f"{entry.get('new_master_collection')} "
                      f"({len(entry.get('carriers_restyled') or [])} 個載體)", flush=True)
        elif terrain_scatter is None:
            entry.update(feasible=False,
                         blocked_reason="場景裡找不到 OpaqueTerrain，沒有地形可以撒")
        else:
            if force_requested and existing_scatter and not DRY_RUN:
                entry["regression_removed_carriers"] = remove_scatter_carriers(
                    existing_scatter
                )
            spec = sc.get(target)
            mod_name = sc.module_path(target)
            cls_name = spec["cls"]
            code_dir = e.get("code")      # ★可選★：給了就用改寫版 factory 生母本
            domain = e.get("domain") or spec["domain"]
            tag = sc.domain_tag(domain)
            entry.update(module=mod_name, cls=cls_name, code_dir=code_dir,
                         domain=domain, tag=tag)
            if DRY_RUN:
                print(f"[extra] (dry) #{idx} create {target} domain={domain}"
                      f"{' ← ' + str(code_dir) if code_dir else ' (官方 factory)'}",
                      flush=True)
            else:
                from infinigen.core.placement import density as _density
                import numpy as _np
                # ★★撒出來的圖樣必須可重現★★(2026-09-07)
                #   apply_plan 開頭只有 random.seed(0)，那是 stdlib random；
                #   scatter 這條路整條吃的是 ★numpy random★ ——
                #       nu.noise(): W=uniform(1e3) 是 4D 噪聲的切片位置，
                #                   每呼叫一次就換一整個噪聲場(不是微擾)
                #       placement_mask(): select_thresh 還會再 normal(.,0.025) 抖動
                #       Factory 母本 seed 也是 np.random.randint
                #   ⇒ 同一個 plan 跑兩次，花海會整片換位置(實測從畫面左邊跑到右邊)。
                #   續跑 step 8 就變成另一張圖，20 個 case 的批次無法重現。
                #   ⚠️ ★只在這裡設種子，不動全域★ —— apply_plan 其他路徑(hero 取樣)
                #      目前的隨機行為維持原樣，避免在批次前改動未驗證的區域。
                _seed_key = f"{target}|{domain}|{idx}"
                _np.random.seed(
                    int(hashlib.sha256(_seed_key.encode()).hexdigest()[:8], 16))
                entry["scatter_seed_key"] = _seed_key
                # ★tag_dict 一定要先灌★ —— 沒灌 tag_mask 會對 None 取 .keys()。
                # ★選不到 key 的 tag 會產生恆為 0 的 mask，撒出 0 個實例而且不報錯★
                #   (forest 場景配 underwater domain 實測就是 0 個 key)。事前擋。
                _keys = sc.tag_keys(_scatter_tag_dict, tag)
                if _scatter_tag_dict and not _keys:
                    entry.update(feasible=False, blocked_reason=(
                        f"domain={domain!r} 的 tag {tag!r} 在這個場景的 MaskTag 裡"
                        f"選不到任何區域(場景可能沒有這種地形) —— 撒出去會是 0 個"))
                    print(f"[extra] ⛔ #{idx} create {target}: {entry['blocked_reason']}",
                          flush=True)
                    report["extra_results"].append(entry)
                    continue
                if _scatter_tag_dict:
                    entry["tag_keys"] = _keys
                    _density.set_tag_dict(_scatter_tag_dict)
                else:
                    entry["selection_degraded"] = (
                        "MaskTag.json unavailable: official noise/slope masks remain, "
                        "but semantic domain tags cannot be evaluated"
                    )

                _base_obj, _base_want = pick_scatter_base(target)
                if _base_obj is not None:
                    terrain_scatter = _base_obj
                    entry["scatter_base"] = terrain_scatter.name
                    entry["scatter_base_official"] = _base_want
                    print(f"[extra]   基底 = {terrain_scatter.name} "
                          f"({len(terrain_scatter.data.vertices)} 頂點, "
                          f"官方 target={_base_want})", flush=True)

                level = (
                    e.get("density_level")
                    or e.get("scatter_density")
                    or sc.DEFAULT_DENSITY_LEVEL
                )
                if level not in sc.DENSITY_LEVELS:
                    print(f"[extra]   ⚠️ density_level={level!r} 不在 "
                          f"{sorted(sc.DENSITY_LEVELS)} 裡 → 用 "
                          f"{sc.DEFAULT_DENSITY_LEVEL}", flush=True)
                    entry["density_level_invalid"] = level
                    level = sc.DEFAULT_DENSITY_LEVEL
                resolved = sc.density_for(target, level)
                entry["density_level"] = level
                entry["density"] = resolved
                if resolved is None:
                    entry["density_note"] = (
                        f"{target} 的 official adapter 不提供 density 旋鈕"
                    )
                else:
                    print(f"[extra]   density {level} → {resolved} "
                          f"(官方基準 {spec['density']})", flush=True)
                if e.get("density") is not None:
                    entry["density_numeric_ignored"] = e.get("density")

                pass_specs = scatter_adapters.passes_for(target, tag, level)
                entry["adapter"] = scatter_adapters.describe(target)
                for pass_spec in pass_specs:
                    mask = pass_spec["mask"]
                    if e.get("mask_scale") is not None:
                        mask["scale"] = float(e["mask_scale"])
                    if e.get("select_thresh") is not None:
                        mask["select_thresh"] = float(e["select_thresh"])
                    if e.get("normal_thresh") is not None:
                        mask["normal_thresh"] = float(e["normal_thresh"])

                invalid_pass_tags = []
                tag_keys_by_pass = {}
                if _scatter_tag_dict:
                    for pass_spec in pass_specs:
                        pass_tag = pass_spec["mask"].get("tag")
                        if not pass_tag:
                            continue
                        pass_keys = sc.tag_keys(_scatter_tag_dict, pass_tag)
                        tag_keys_by_pass[pass_spec["name"]] = pass_keys
                        if not pass_keys:
                            invalid_pass_tags.append({
                                "pass": pass_spec["name"],
                                "tag": pass_tag,
                            })
                if invalid_pass_tags:
                    entry.update(
                        feasible=False,
                        blocked_reason=(
                            "official adapter 的 stage tag 在 MaskTag 裡選不到區域: "
                            f"{invalid_pass_tags}"
                        ),
                        invalid_pass_tags=invalid_pass_tags,
                    )
                    print(f"[extra] ⛔ #{idx} create {target}: "
                          f"{entry['blocked_reason']}", flush=True)
                    report["extra_results"].append(entry)
                    continue
                entry["tag_keys_by_pass"] = tag_keys_by_pass

                placement_spec = e.get("placement_spec") or {}
                surface_policy = placement_spec.get("surface_policy") or {}
                required_surface_types = [
                    str(value).strip().lower()
                    for value in surface_policy.get(
                        "required_surface_types", spec.get("surfaces") or ()
                    )
                    if str(value).strip()
                ]
                unsupported_surfaces = sorted(
                    set(required_surface_types) - set(spec.get("surfaces") or ())
                )
                if unsupported_surfaces:
                    entry.update(
                        feasible=False,
                        blocked_reason=(
                            f"{target} 不支援 director 選的 surface types: "
                            f"{unsupported_surfaces}"
                        ),
                    )
                    report["extra_results"].append(entry)
                    continue

                camera_policy = str(
                    e.get("camera_visibility_policy", "required")
                ).strip().lower()
                if camera_policy not in {"required", "off"}:
                    camera_policy = "required"
                anchors = []
                if camera_policy == "required":
                    anchors = scatter_visibility.collect_visible_surface_anchors(
                        scene,
                        cam,
                        surface_scene_name,
                        required_surface_types,
                        columns=int(e.get("camera_anchor_columns", 9)),
                        rows=int(e.get("camera_anchor_rows", 6)),
                    )
                    if not anchors:
                        entry.update(
                            feasible=False,
                            blocked_reason=(
                                "相機射線找不到符合 surface policy 的可見地表；"
                                "拒絕產生畫面外 scatter"
                            ),
                        )
                        report["extra_results"].append(entry)
                        continue
                entry["camera_visibility_policy"] = camera_policy
                entry["camera_surface_types"] = required_surface_types
                entry["camera_visible_anchor_count"] = len(anchors)
                entry["camera_visible_anchors"] = anchors
                entry["surface_semantic_resolution"] = (
                    "mesh-level registry; material subregions are not encoded per face"
                )

                # ★官方 LOD 分塊不一定蓋得住相機可見帶★(2026-09-09 量測)
                #   forest/seed0：inview_inview 只到 43.0m，而相機看得到的地面
                #   一路到 73.9m ⇒ 52 個 anchor 有 19 個(全部 >44m)打在這張網格
                #   外面，完全沒有東西可撒。畫面上就是「草長到一半齊頭切斷」
                #   —— 那條銜接線不是遮罩邊界，是★LOD 分塊的邊緣★。
                #   官方 target 仍是第一順位，只有在它蓋不住可見帶時才往外擴。
                #   ★而且「往外擴」必須擴到★相機真的看到的那一張網格★上★
                #   (2026-09-09 fine 實測)：fine 場景同時存在 OpaqueTerrain_fine
                #   跟 coarse 的 OpaqueTerrain。_BASE_OBJ 只認得後者 ⇒ 862,809 株
                #   草被撒在 coarse 地形上、整個埋在 fine 地表底下，成圖是一片
                #   光禿禿的土 —— 這就是「草陷在地裡」的真正機制。anchor 已經
                #   記下自己打在哪個物件上，那就是相機看得到的表面，直接用它。
                if anchors and _SCATTER_BASE == "":
                    _hit_names = []
                    for _row in anchors:
                        if _row["object"] not in _hit_names:
                            _hit_names.append(_row["object"])
                    _candidates = []
                    for _name in (terrain_scatter.name, *_hit_names,
                                  _BASE_OBJ["inview"],
                                  _BASE_OBJ["near"], _BASE_OBJ["terrain"]):
                        _obj = next(
                            (o for o in bpy.data.objects if o.name == _name), None)
                        if _obj is not None and _obj not in _candidates:
                            _candidates.append(_obj)
                    _picked, _reach = scatter_visibility.pick_base_covering_anchors(
                        _candidates, anchors)
                    entry["scatter_base_reach"] = _reach
                    if _picked is not None and _picked is not terrain_scatter:
                        entry["scatter_base_widened_from"] = terrain_scatter.name
                        terrain_scatter = _picked
                        entry["scatter_base"] = terrain_scatter.name
                        print(f"[extra]   ⤢ 基底改用 {terrain_scatter.name} "
                              f"(官方 target={_base_want} 蓋不住相機可見帶)",
                              flush=True)

                clear_m = float(e.get("camera_clear_m", DEFAULT_CAMERA_CLEAR_M))
                far_m = e.get("camera_far_m", DEFAULT_CAMERA_FAR_M)
                cone = e.get("camera_cone_deg")
                cos_half = (
                    math.cos(math.radians(float(cone)))
                    if cone is not None else None
                )
                entry["camera_clear_m"] = clear_m
                if far_m is not None:
                    entry["camera_far_m"] = float(far_m)
                if cone is not None:
                    entry["camera_cone_deg"] = float(cone)

                # 官方的邊緣淡出係數在遮罩邊界正好歸零，而 coverage 降門檻爭取
                # 到的面積剛好全在那一段 ⇒ 撒得出來但小到看不見。抬下限。
                taper_floor_value = float(
                    e.get("taper_floor", sc.taper_floor(target))
                )
                entry["taper_floor"] = taper_floor_value

                built_masks = []
                def build_adapter_selection(mask_spec, pass_name):
                    mask_kwargs = dict(mask_spec)
                    pass_tag = mask_kwargs.get("tag")
                    if not _scatter_tag_dict and pass_tag is not None:
                        mask_kwargs.pop("tag", None)
                    selection = _density.placement_mask(**mask_kwargs)
                    if clear_m > 0 or far_m is not None or cos_half is not None:
                        selection = camera_focus_selection(
                            selection,
                            campos,
                            camfwd,
                            clear_m if clear_m > 0 else None,
                            float(far_m) if far_m is not None else None,
                            cos_half,
                        )
                    if anchors:
                        selection = scatter_visibility.restrict_to_visible_anchors(
                            selection, anchors
                        )
                    if taper_floor_value > 0:
                        selection = scatter_visibility.raise_taper_floor(
                            selection, taper_floor_value
                        )
                    built_masks.append({
                        "pass": pass_name,
                        "params": mask_kwargs,
                        "taper_floor": taper_floor_value,
                    })
                    return selection

                # Load and patch an optional generated master before importing
                # the scatter module. The overlay loader purges cached Infinigen
                # modules, so reversing this order leaves a stale module object.
                master_override = None
                symbol = spec["factory_symbol"] if code_dir else None
                if code_dir:
                    if not symbol:
                        entry["appearance_degraded"] = (
                            f"{target} 沒有可替換的 factory 符號，使用官方外觀"
                        )
                    else:
                        FacClass, fqn = load_overlay_factory(code_dir)
                        master_override = ground_aligned_factory(FacClass)
                        entry.update(
                            fqn=fqn,
                            patched_symbol=symbol,
                            ground_aligned=True,
                        )
                mod = importlib.import_module(mod_name)
                if master_override is not None:
                    setattr(mod, symbol, master_override)
                # 個別母本生成失敗不要炸掉整批(見 tolerant_make_asset_collection)
                if hasattr(mod, "make_asset_collection"):
                    mod.make_asset_collection = tolerant_make_asset_collection(
                        mod.make_asset_collection, entry,
                        scale=asset_scale_for(e.get("code_from"), target))

                minimum_visible = int(e.get(
                    "camera_min_effectively_visible",
                    sc.min_effectively_visible(target, level),
                ))

                # ★coverage 安全網★(2026-09-09)
                #   官方遮罩是低頻大塊 noise，相機有沒有對到通過的那一塊靠運氣
                #   ⇒ 建完就量畫面內有效實例，不足就沿★官方自己的旋鈕★
                #   (select_thresh，density level 用的同一個)往下疊一階重建。
                #   ⚠️ 動的不是 density —— ferns 的官方密度已經是 20 株/m²，
                #      瓶頸從來不在每平方公尺幾株，而在有多少面積通過遮罩。
                ladder = sc.coverage_escalation_thresholds(target, level)
                if (not ladder or camera_policy == "off"
                        or e.get("select_thresh") is not None):
                    # 明確指定 select_thresh 的 edit 就是要那個值，
                    # 安全網不能在背後把它改掉。
                    ladder = [None]
                entry["coverage_escalation_ladder"] = ladder

                # ★母本縮放這一軸★(2026-09-10)：降門檻救不了「密度被母本體積
                #   除掉」的崩潰 —— 降門檻是★增加面積★，但那時每平方公尺密度
                #   已經 ≈0。實測 pine_needle←cattail_reed：門檻 0.175→0.05
                #   之後總數反而從 4 掉到 1。
                #   ⇒ 面積用完之後再往「把母本縮小」走(密度隨 scale² 上升)。
                #   ⚠️ 只有★量到不夠★才會進到這一軸；向日葵那種
                #     effective 1765 ≫ 門檻 40 的，第 0 階就達標，
                #     「大朵花本來就該少一點」完全保留。
                # ★已實測生效★(2026-09-10 grass←silvergrass)：
                #    第 0 階 thresh=0.075 scale=1.0 → total=107  effective=38 (需要 40)
                #    第 1 階 thresh=0.05  scale=1.0 → total=67   effective=24 ← ★降門檻反而更少★
                #    第 2 階 thresh=0.05  scale=0.5 → total=2165 effective=749 ← 達標
                #    ⇒ 對「密度被母本體積除掉」的案例，★覆蓋率那一軸沒用甚至更糟★，
                #      只有縮小母本救得回來(密度隨 scale² 上升)。
                # 正常路徑不受影響：flowers←sunflower 實測 effective=834 遠超門檻，
                # 第 0 階就達標、master_scale_degraded=False，大朵花的行為完整保留。
                master_scales = ([1.0] if master_override is None
                                 else [1.0, 0.5, 0.25])
                if e.get("master_scale") is not None:
                    master_scales = [float(e["master_scale"])]
                entry["master_scale_ladder"] = master_scales

                def _discard(carriers, collections):
                    # 丟掉的那一階要連 make_asset_collection 建的母本一起收，
                    # 否則每退一階就把一組沒人用的母本永久留在 .blend 裡。
                    for obj in carriers:
                        try:
                            bpy.data.objects.remove(obj, do_unlink=True)
                        except (ReferenceError, RuntimeError):
                            pass
                    for col in collections:
                        try:
                            butil.delete_collection(col)
                        except (ReferenceError, RuntimeError, AttributeError):
                            pass

                attempts = []
                result_carriers = []
                result_collections = []
                result_masks = []
                serializable_records = []
                applied_thresh = None
                applied_master_scale = 1.0
                metrics = None
                # ★先走完覆蓋率這一軸，再走母本縮放★ —— 覆蓋率是官方自己的
                #   旋鈕(保留官方外觀)，縮母本是我們額外加的補救，順序不能反。
                plan_steps = [(t, 1.0) for t in ladder]
                for _msc in master_scales[1:]:
                    plan_steps.append((ladder[-1], _msc))
                for step, (thresh, master_scale) in enumerate(plan_steps):
                    # 每一階都用同一顆種子，讓「第 N 階」本身是可重現的。
                    _np.random.seed(int(hashlib.sha256(
                        f"{_seed_key}|{step}".encode()).hexdigest()[:8], 16))
                    built_masks.clear()
                    if master_override is not None:
                        master_override._bi_master_scale = master_scale
                    for pass_spec in pass_specs:
                        if thresh is not None and (
                                "select_thresh" in pass_spec["mask"]):
                            pass_spec["mask"]["select_thresh"] = thresh

                    before_objects = set(bpy.data.objects)
                    adapter_records = scatter_adapters.execute(
                        mod,
                        cls_name,
                        terrain_scatter,
                        pass_specs,
                        build_adapter_selection,
                        season=e.get("season") or scene.get("season"),
                    )
                    new_carriers = [
                        obj for obj in bpy.data.objects
                        if obj not in before_objects
                        and obj.name.startswith("scatter:")
                    ]
                    carriers = []
                    collections = []
                    records = []
                    for record in adapter_records:
                        result = record.pop("result")
                        scatter_obj = (
                            result[0] if isinstance(result, tuple) else result
                        )
                        if isinstance(result, tuple) and len(result) > 1:
                            collections.extend(
                                item for item in result[1:]
                                if isinstance(item, bpy.types.Collection)
                            )
                        if scatter_obj is not None:
                            carriers.extend(stamp_scatter_carriers(
                                [scatter_obj], target, record["name"]
                            ))
                        record["created_object"] = (
                            scatter_obj.name if scatter_obj is not None else None
                        )
                        records.append(record)
                    stamped_ids = {obj.as_pointer() for obj in carriers}
                    carriers.extend(stamp_scatter_carriers(
                        [obj for obj in new_carriers
                         if obj.as_pointer() not in stamped_ids],
                        target, "adapter-detected",
                    ))

                    step_metrics = scatter_visibility.audit_visibility(
                        scene,
                        cam,
                        carriers,
                        min_pixels=float(e.get(
                            "camera_effective_min_pixels", 2.0
                        )),
                    )
                    attempts.append({
                        "step": step,
                        "select_thresh": thresh,
                        "master_scale": master_scale,
                        "total": step_metrics["total"],
                        "in_frustum": step_metrics["in_frustum"],
                        "effectively_visible":
                            step_metrics["effectively_visible"],
                    })
                    print(f"[extra]   第 {step} 階 select_thresh={thresh} "
                          f"master_scale={master_scale} → "
                          f"total={step_metrics['total']} "
                          f"effective={step_metrics['effectively_visible']} "
                          f"(需要 {minimum_visible})", flush=True)

                    enough = (
                        step_metrics["effectively_visible"] >= minimum_visible
                    )
                    # 撐不住就換下一階；但★寧可留住目前最好的一階★，
                    # 而不是丟掉全部只剩空場景。
                    better = (
                        metrics is None
                        or step_metrics["effectively_visible"]
                        > metrics["effectively_visible"]
                    )
                    if better:
                        _discard(result_carriers, result_collections)
                        result_carriers = carriers
                        result_collections = collections
                        result_masks = list(built_masks)
                        serializable_records = records
                        metrics = step_metrics
                        applied_thresh = thresh
                        applied_master_scale = master_scale
                    else:
                        _discard(carriers, collections)
                    if enough:
                        break

                entry["coverage_attempts"] = attempts
                official_thresh = sc.select_thresh_for(target, level)
                entry["select_thresh_official"] = official_thresh
                entry["master_scale_applied"] = applied_master_scale
                entry["master_scale_degraded"] = bool(
                    abs(applied_master_scale - 1.0) > 1e-9)
                entry["select_thresh_applied"] = applied_thresh
                entry["deviates_from_official"] = bool(
                    applied_thresh is not None
                    and official_thresh is not None
                    and abs(applied_thresh - official_thresh) > 1e-9
                )
                entry["adapter_passes"] = serializable_records
                entry["mask_params_by_pass"] = result_masks
                entry["mask_params"] = (
                    result_masks[0]["params"] if result_masks else {}
                )
                entry["mask_params_from"] = "official_adapter+level+coverage"
                entry["created_objects"] = [obj.name for obj in result_carriers]
                entry["created_object"] = (
                    result_carriers[0].name if result_carriers else None
                )

                entry["instance_metrics"] = metrics
                entry["total"] = metrics["total"]
                entry["in_frustum"] = metrics["in_frustum"]
                entry["effectively_visible"] = metrics["effectively_visible"]
                guarantee_passed = (
                    camera_policy == "off"
                    or metrics["effectively_visible"] >= minimum_visible
                )
                entry["camera_min_effectively_visible"] = minimum_visible
                entry["visibility_guarantee_passed"] = guarantee_passed
                if not guarantee_passed:
                    entry.update(
                        feasible=False,
                        blocked_reason=(
                            "scatter 已建立但相機可見性 contract 失敗: "
                            f"effectively_visible={metrics['effectively_visible']} "
                            f"< required={minimum_visible}"
                        ),
                    )
                print(
                    f"[extra] {'✅' if guarantee_passed else '⛔'} #{idx} "
                    f"create {target} → {entry['created_objects']} "
                    f"total={metrics['total']} "
                    f"in-frustum={metrics['in_frustum']} "
                    f"effective={metrics['effectively_visible']}",
                    flush=True,
                )
        report["extra_results"].append(entry)
        continue

    scats = find_scatter(target)
    if not scats:
        avail = [o.name for o in bpy.data.objects if o.name.startswith("scatter:")]
        entry.update(feasible=False,
                     blocked_reason=f"沒有 scatter:*{target}*。場景有: {avail}。"
                                    f"若要新建請用 op='create'")
        print(f"[extra] ⛔ #{idx} {op}:{target} —— 找不到", flush=True)
        report["extra_results"].append(entry)
        continue

    scat = scats[0]
    md = gn_of(scat)
    entry["scatter_object"] = scat.name
    entry["scatter_objects"] = [obj.name for obj in scats]
    entry["semantic_scatter_id"] = sc.canonical_semantic_id(target)

    if op in ("delete", "density"):
        changes = []
        for scatter_obj in scats:
            scatter_md = gn_of(scatter_obj)
            sock = density_socket(scatter_md) if scatter_md else None
            if sock is None:
                changes.append({
                    "object": scatter_obj.name,
                    "error": "Overall Density socket missing",
                })
                continue
            old = float(scatter_md[sock])
            new = 0.0 if op == "delete" else float(e.get("value", old))
            if not DRY_RUN:
                scatter_md[sock] = new
                scatter_obj[sc.SEMANTIC_ID_PROPERTY] = target
            changes.append({
                "object": scatter_obj.name,
                "density_before": old,
                "density_after": new,
            })
        entry["density_changes"] = changes
        entry["density_before"] = (
            changes[0].get("density_before") if changes else None
        )
        entry["density_after"] = (
            changes[0].get("density_after") if changes else None
        )
        print(f"[extra] ✅ #{idx} {op} {target}: "
              f"{len(changes)} semantic carriers updated", flush=True)

    elif op == "scale":
        # ★只能整批★ —— 改母本的 scale，全場一起變
        v = float(e.get("value", 1.0))
        inp = master_input(md)
        col = inp.default_value if inp else None
        n = 0
        if col:
            for ob in col.objects:
                if not DRY_RUN:
                    ob.scale = (ob.scale.x * v, ob.scale.y * v, ob.scale.z * v)
                n += 1
        entry.update(scaled_masters=n, value=v,
                     note="★EXTRA 的 scale 只能整批★(改母本)，無法只縮某一叢")
        print(f"[extra] ✅ #{idx} scale {target}: {n} 個母本 ×{v}", flush=True)

    elif op == "replace":
        code_dir = Path(e["code"])
        if not (code_dir / "factory.txt").exists():
            entry.update(feasible=False,
                         blocked_reason=f"{code_dir}/factory.txt 不存在")
        else:
            restyle_scatter(scats, code_dir, e.get("n_masters"), entry, target,
                            code_from=e.get("code_from"))
            if DRY_RUN:
                print(f"[extra] (dry) #{idx} replace {target} ← {code_dir}", flush=True)
            else:
                print(f"[extra] ✅ #{idx} replace {target}: "
                      f"{entry.get('old_master_collection') or '?'} → "
                      f"{entry.get('new_master_collection')} "
                      f"({len(entry.get('carriers_restyled') or [])} 個載體)", flush=True)

    report["extra_results"].append(entry)


# ════════════════════════════════════════════════════════ 官方魚群(swarm_edits)
#   (2026-09-11) 魚走官方 boid 魚群，不再 hero add 3-5 條(0910 coral_reef：3 條小丑魚
#   在 8-12m 外只剩幾個像素，旁邊官方魚群有幾百條)。
#     replace  這裡只記下來，manifest 那一列蓋 code_from ⇒ fillcode 填 code ⇒
#              placer.master_swap 換粒子系統的 instance_collection(數量/群游不動)
#     add      照 generate_nature.add_fish_school 建一群：FishSchoolFactory 的參數、
#              volume、boid 規則全用官方的，只把 3 條母本換成 overlay 的魚，
#              落點改成相機前方幾公尺(官方是 terrain_near 上隨機)。
def _school_location(k, n_schools, prepared):
    """相機前方、畫面內、離地 >= 2m、視線不被地形擋住的一點。"""
    right = camfwd.cross(Vector((0, 0, 1)))
    right = right.normalized() if right.length > 1e-6 else Vector((1, 0, 0))
    lateral = (k - (n_schools - 1) / 2.0) * 0.35
    # ★由遠而近★(2026-09-12 under_water：9m 的一整群 1.3m 大魚糊住整個畫面)。
    #   官方魚群的 4m 發射盒在 9m 處就佔掉半個畫面；先試 18m，不行才往前。
    for d in (18.0, 14.0, 22.0, 11.0, 28.0, 8.0):
        cand = campos + camfwd * d + right * (lateral * d)
        ground = pm.terrain_z_at(prepared, cand.x, cand.y, cand.z) if prepared else None
        if ground is not None:
            if ground + 2.0 > campos.z + 3.0:
                continue                      # 那裡的地面比相機還高 ⇒ 會埋進地形
            cand.z = max(cand.z, ground + 2.0)
        cand.z = min(cand.z, campos.z + 3.0)
        blocked = False
        for t in (terrain or []):
            mwi = t.matrix_world.inverted()
            seg = cand - campos
            try:
                ok, *_ = t.evaluated_get(bpy.context.evaluated_depsgraph_get()).ray_cast(
                    mwi @ campos, (mwi.to_3x3() @ seg).normalized(),
                    distance=(mwi.to_3x3() @ seg).length)
            except (RuntimeError, ValueError):
                continue
            blocked = blocked or ok
        if not blocked and _in_camera_view(cand):
            return cand, d
    return campos + camfwd * 8.0, None


def add_fish_schools(e):
    code_dir = Path(str(e.get("code") or ""))
    if not (code_dir / "factory.txt").is_file():
        raise FileNotFoundError(f"swarm add 的 overlay 不存在: {code_dir}")
    n_schools = max(1, min(int(e.get("count") or 1), 3))
    # ★用官方原本的 volume，不再乘語意等級★(2026-09-12 under_water：blue_tang 這種
    #   1.3m 的大魚，dense 讓 347 條擠在鏡頭前 9m 糊住整個畫面)。官方公式
    #   count = volume / (avg_size³ × 魚體積) 本來就會依魚大小調數量，等級再乘一次就爆。
    #   密度語意改用「群的大小」表達：sparse/normal/dense 只影響 volume ±25%。
    volume_scale = {"sparse": 0.75, "normal": 1.0, "dense": 1.25}.get(
        osc.level_of(e.get("density_level")), 1.0)
    if DRY_RUN:
        return {"dry_run": True, "n_schools": n_schools}
    import mathutils
    FishCls, fqn = load_overlay_factory(code_dir, _overlay_declared_factory(code_dir))
    # overlay 已經 purge + 插到 sys.path 最前面 ⇒ 這裡 import 到的是同一套模組
    from numpy.random import randint as _randint
    from infinigen.assets.objects.creatures import fish as _fish
    from infinigen.assets.objects.creatures.util.boid_swarm import BoidSwarmFactory
    from infinigen.core.placement import placement as _placement
    from infinigen.core.placement.factory import make_asset_collection
    from infinigen.core.util import blender as _butil
    from infinigen.core.util.math import FixedSeed

    def _init(self, factory_seed, bvh=None, coarse=False):
        # = fish.FishSchoolFactory.__init__，只把 FishFactory 換成 overlay 的魚
        with FixedSeed(factory_seed):
            settings = self.fish_school_params()
            col = make_asset_collection(
                FishCls(factory_seed=_randint(1e7), animation_mode="idle"),
                n=int(e.get("n_masters") or 3))
        BoidSwarmFactory.__init__(
            self, factory_seed, child_col=col,
            collider_col=bpy.data.collections.get("colliders"),
            settings=settings, bvh=bvh,
            volume=("uniform", 3 * volume_scale, 10 * volume_scale), coarse=coarse)

    # 類名維持 FishSchoolFactory ⇒ collection 名與官方魚群同形，下游 manifest/驗證通吃
    School = type("FishSchoolFactory", (_fish.FishSchoolFactory,), {"__init__": _init})
    deps = bpy.context.evaluated_depsgraph_get()
    meshes = [t for t in (terrain or []) if t.type == "MESH"]
    bvh = (mathutils.bvhtree.BVHTree.FromObject(meshes[0], deps) if meshes else None)
    prepared = pm.prepare_terrains(terrain) if terrain else None
    schools = []
    for k in range(n_schools):
        loc, dist = _school_location(k, n_schools, prepared)
        fac = School(int(_randint(1e7)), bvh=bvh)
        ph = fac.spawn_placeholder(0, loc, Euler((0.0, 0.0, 0.0)))
        col = _butil.group_in_collection([ph], "placeholders:" + repr(fac))
        _placement.populate_collection(fac, col)
        ucol = bpy.data.collections.get(f"unique_assets:{fac!r}")
        emitters = [o for o in (ucol.objects if ucol else [])
                    if getattr(o, "particle_systems", None) and len(o.particle_systems)]
        if not emitters:
            raise RuntimeError(f"{fac!r}: populate 後找不到帶粒子系統的 emitter")
        emitters[0]["bi_added_swarm_from"] = e.get("code_from")
        count = int(emitters[0].particle_systems[0].settings.count)
        schools.append({"school": repr(fac), "emitter": emitters[0].name,
                        "location": [round(float(v), 2) for v in loc],
                        "distance_m": dist, "particle_count": count})
        print(f"[swarm] ✅ {e.get('code_from')}: 新魚群 {fac!r} 在相機前 "
              f"{dist if dist is not None else '8(fallback)'}m，{count} 條", flush=True)
    return {"fqn": fqn, "n_schools": n_schools, "schools": schools}


# ════════════════════════════════════════════════════════ 官方撒點(official_scatter_edits)
#   (2026-09-11) 植物/石頭的 add 不再走 hero grid(一格一個、1-3 棵、Step 8 放不下的大宗)。
#   照 generate_nature 的 add_trees/add_bushes/add_boulders…：placeholder_locs 的
#   Poisson 密度 + 最小間距，再用 overlay 自己的 spawn_placeholder 建 box(樹有真骨架)。
#   ★只多一個「相機附近」的窗★：距離 3m..max_dist、畫面外擴 15%，並避開既有 box。
#   這批★不算 added_this_plan★ ⇒ manifest 當成「有 code 的 collection」(requested_replace)：
#   placer 只 populate 相機看得到的那些，也不跑逐一「必須入鏡」的 hero 關卡。
official_scatter_attrs = {}


def _terrain_for_scatter():
    """★撒點的基底要用面積最大的那塊地形★(2026-09-11)。

    ⚠️ 不要照名字挑 `OpaqueTerrain.inview_near` —— 那是相機正前方的 LOD 小塊，
       canyon 實測只有 ★83 m²★(整片 OpaqueTerrain 是 36457 m²)，density 0.04 撒出來
       只有 5 個點、全部落在畫面外，三個資產一起失敗。「只撒在相機附近」是由下面的
       距離 + 視錐過濾負責的，不是靠挑小塊地形。
    """
    from infinigen.core.util import blender as _butil
    meshes = [t for t in (terrain or []) if t.type == "MESH"]

    def area(mesh):
        try:
            return float(_butil.surface_area(mesh))
        except Exception:
            return float(len(mesh.data.polygons))

    return max(meshes, key=area, default=None)


# 離水面這麼近也算「在水裡」——0911 coast seed0：棕櫚樹剛好長在水線上，
# 樹幹泡在水裡。官方的遮罩是整片地形標籤，沒有這個問題。
WATER_MARGIN_M = 0.6


def _is_underwater(p, liquids, deps):
    for liquid in liquids:
        mwi = liquid.matrix_world.inverted()
        probe = Vector((p.x, p.y, p.z + WATER_MARGIN_M))
        try:
            ok, *_ = liquid.evaluated_get(deps).ray_cast(
                mwi @ probe, (mwi.to_3x3() @ Vector((0, 0, 1))).normalized(),
                distance=500.0)
        except (RuntimeError, ValueError):
            continue
        if ok:
            return True
    return False


def add_official_scatter(e):
    from bpy_extras.object_utils import world_to_camera_view
    import mathutils
    code_dir = Path(str(e.get("code") or ""))
    if not (code_dir / "factory.txt").is_file():
        raise FileNotFoundError(f"official_scatter 的 overlay 不存在: {code_dir}")
    declared = _overlay_declared_factory(code_dir)
    prm = osc.params_for(declared, e.get("density_level"))
    base = _terrain_for_scatter()
    if base is None:
        raise RuntimeError("找不到可撒點的地形 mesh")
    if DRY_RUN:
        return {"dry_run": True, "factory": declared, **prm}
    FacClass, fqn = load_overlay_factory(code_dir, declared)
    from numpy.random import randint as _randint
    from infinigen.core.placement import placement as _placement

    locs = _placement.placeholder_locs(
        base, prm["density"], 1, distance_min=prm["distance_min"],
        altitude=prm["altitude"])
    deps = bpy.context.evaluated_depsgraph_get()
    liquids = [o for o in bpy.data.objects
               if o.type == "MESH" and o.name.lower().startswith("liquid")]
    occupied = mathutils.kdtree.KDTree(max(1, len(added_names) + 4096))
    n_occ = 0
    for c in bpy.data.collections:
        if PH_COLL_RE.fullmatch(c.name):
            for o in c.objects:
                if o.parent is None and n_occ < 4096 + len(added_names):
                    occupied.insert(o.matrix_world.translation, n_occ)
                    n_occ += 1
    occupied.balance()
    cos_slope = math.cos(math.radians(prm["max_slope"]))
    scene.frame_set(CAM_FRAME)
    # 相機看得到的點先依距離排好；窗口由近到遠放寬(0911 coast seed0：相機朝海，
    # 45m 內畫面裡沒有陸地 ⇒ 1689 點全被濾掉)。「近」是偏好，「在畫面內」才是硬要求。
    visible = []
    why = {}
    for raw in locs:
        p = Vector(raw)
        d = (p - campos).length
        co = world_to_camera_view(scene, cam, p)
        if d < 3.0 or co.z <= 0 or not (-0.15 < co.x < 1.15 and -0.15 < co.y < 1.15):
            why["off_camera"] = why.get("off_camera", 0) + 1
            continue
        visible.append((d, p, co))
    visible.sort(key=lambda item: item[0])
    nearest_visible = round(visible[0][0], 1) if visible else None
    in_frame, near_frame = [], []
    max_dist = prm["max_dist"]
    for scale in (1.0, 1.5, 2.5):
        max_dist = prm["max_dist"] * scale
        in_frame, near_frame = _filter_scatter_points(
            [item for item in visible if item[0] <= max_dist], prm, occupied, n_occ,
            cos_slope, liquids, deps, why)
        if len(in_frame) >= osc.MIN_IN_FRAME:
            break
    # 近的優先：先留最近的 2×cap(它們在畫面上才夠大)，再隨機取 cap 保持官方的散佈感。
    # 不直接取最近的 cap 個 —— 那會全部擠在相機腳邊。
    in_frame = in_frame[:prm["cap"] * 2]
    near_frame = near_frame[:prm["cap"] * 2]
    random.shuffle(in_frame)
    random.shuffle(near_frame)
    chosen = (in_frame + near_frame)[:prm["cap"]]
    n_in = min(len(in_frame), prm["cap"])
    if n_in < osc.MIN_IN_FRAME:
        raise RuntimeError(
            f"{declared}: {max_dist:.0f}m 內只找到 {len(in_frame)} 個畫面內的合格點"
            f"(需要 >= {osc.MIN_IN_FRAME})；Poisson {len(locs)} 點，"
            f"最近的可見點 {nearest_visible}m，濾掉 {why}")
    try:
        fac = FacClass(int(_randint(1e7)), coarse=True)
    except TypeError:
        fac = FacClass(int(_randint(1e7)))
    col = _placement.scatter_placeholders([tuple(p) for p in chosen], fac)
    for o in col.objects:
        if o.parent is None:
            stamp_anchor(o, "rooted", float(o.rotation_euler.z))
    collection_code_intent[col.name] = e.get("code_from")
    official_scatter_attrs[col.name] = {"anchoring": "rooted",
                                        "n_masters": int(e.get("n_masters") or 5)}
    print(f"[official_scatter] ✅ {e.get('code_from')}: {col.name} {len(chosen)} 個 "
          f"(畫面內 {n_in}，窗口 {max_dist:.0f}m) group={prm['group']} "
          f"level={prm['level']} density={prm['density']:.3f}/m² 濾掉 {why}", flush=True)
    return {"fqn": fqn, "collection": col.name, "placed": len(chosen), "in_frame": n_in,
            "window_m": round(max_dist, 1), "nearest_visible_m": nearest_visible,
            "poisson_points": len(locs), "filtered": why,
            **{k: prm[k] for k in ("group", "level", "density", "distance_min")}}


def _filter_scatter_points(visible, prm, occupied, n_occ, cos_slope, liquids, deps, why):
    """Occupied / slope / land-vs-water filter over (distance, point, ndc) triples."""
    in_frame, near_frame = [], []
    for _d, p, co in visible:
        if n_occ and occupied.find(p)[2] is not None and occupied.find(p)[2] < prm["clearance"]:
            why["occupied"] = why.get("occupied", 0) + 1
            continue
        normal, _z = pm.support_normal(terrain, p, 0.0)
        if normal is not None and abs(normal.z) < cos_slope:
            why["slope"] = why.get("slope", 0) + 1
            continue
        if prm["domain"] != "any" and liquids:
            under = _is_underwater(p, liquids, deps)
            if under != (prm["domain"] == "underwater"):
                why["domain"] = why.get("domain", 0) + 1
                continue
        (in_frame if 0 < co.x < 1 and 0 < co.y < 1 else near_frame).append(p)
    return in_frame, near_frame


report["official_scatter_results"] = []
for idx, e in enumerate(official_scatter_edits):
    entry = {"idx": idx, "code_from": e.get("code_from"), "feasible": True}
    try:
        entry.update(add_official_scatter(e))
    except Exception as ex:
        entry.update(feasible=False, blocked_reason=f"{type(ex).__name__}: {ex}")
        print(f"[official_scatter] ⛔ #{idx} {e.get('code_from')}: {ex}", flush=True)
    report["official_scatter_results"].append(entry)
_scatter_failed = [r for r in report["official_scatter_results"] if not r["feasible"]]
if _scatter_failed:
    raise SystemExit(f"[official_scatter] {len(_scatter_failed)} 筆官方撒點失敗: "
                     f"{[r.get('blocked_reason') for r in _scatter_failed]}")


swarm_replace_by_target = {}
swarm_replace_by_family = {}


def _swarm_replace_for(key: str) -> dict | None:
    exact = swarm_replace_by_target.get(str(key))
    if exact is not None:
        return exact
    family = str(key).split("(", 1)[0]
    return swarm_replace_by_family.get(family)


report["swarm_results"] = []
for idx, e in enumerate(swarm_edits):
    entry = {"idx": idx, "op": e.get("op"), "target": e.get("target"),
             "code_from": e.get("code_from"), "feasible": True}
    if e.get("op") == "replace":
        swarm_replace_by_target[str(e.get("target"))] = e
        swarm_replace_by_family.setdefault(str(e.get("target")).split("(", 1)[0], e)
        entry["note"] = "masters are swapped at Step 9 (placer.master_swap)"
    elif e.get("op") == "add":
        try:
            entry.update(add_fish_schools(e))
        except Exception as ex:
            entry.update(feasible=False, blocked_reason=f"{type(ex).__name__}: {ex}")
            print(f"[swarm] ⛔ #{idx} add {e.get('code_from')}: {ex}", flush=True)
    else:
        entry.update(feasible=False, blocked_reason=f"swarm op {e.get('op')!r} 不支援")
    report["swarm_results"].append(entry)
_swarm_failed = [r for r in report["swarm_results"] if not r["feasible"]]
if _swarm_failed:
    # 魚群沒建成 = 這隻魚沒進場景；不要默默出一張沒有魚的圖
    raise SystemExit(f"[swarm] {len(_swarm_failed)} 筆魚群編輯失敗: "
                     f"{[r.get('blocked_reason') for r in _swarm_failed]}")


# ════════════════════════════════════════════════════════ 結構驗證 + 存檔
structural = {"checked": False}
if not DRY_RUN and added_names:
    problems = []
    for nm in added_names:
        o = bpy.data.objects.get(nm)
        if o is None:
            problems.append((nm, "物件不存在"))
        elif (len(o.children) == 0
              and any(nm.startswith(prefix + "(") for prefix in {
                  "TreeFactory", "CachedTreeFactory", "BushFactory", "CachedBushFactory",
              })):
            problems.append((nm, "★沒有 children 骨架 → populate 會 IndexError★"))
    structural = {"checked": True, "added": len(added_names),
                  "problems": problems, "ok": not problems}
    print(f"[plan] 結構檢查: {len(added_names)} 新增, 問題 {len(problems)}", flush=True)
report["structural_check"] = structural
report["added_placeholder_names"] = added_names

# ════════════════════════════════════════════════════════
# ★★產出 placement_manifest —— 階段 A → 階段 C 的橋樑★★
#   內容 = 「這份 coarse blend 裡有哪些 box、各需要哪個 Factory」
#   ⇒ asset generator(階段 B) 讀它知道要生什麼；
#     placer.py(階段 C) 讀它把 code 塞進對應的 box。
#   ★"code" 欄位留空，由階段 B 填★ —— 不填就用官方 factory。
#   ⚠️ ★這一步不需要 call API★：box 有哪些、各多少個、in-view 幾個，
#      全部是掃 blend 算得出來的確定性資訊。
# ════════════════════════════════════════════════════════
# ★★plan 裡「針對某個 factory」的擺放屬性★★(2026-08-05 補)
#   ⚠️ ★為什麼非補不可★：manifest 的 anchoring 原本★只從 box 的 bi_anchoring 推★，
#      而 bi_anchoring 只有 add 路徑會 stamp。★hero 的 replace 根本不是 op★
#      (HERO_OPS = delete/scale/move/add)，那些 box 從頭到尾沒被階段 A 碰過
#      ⇒ anchoring 一路是 None ⇒ 階段 C 的 settle_targets 收不到它
#      ⇒ ★整個替換完全不做貼地，而且不報錯★。
#      (先前摩托車測試會貼地，是因為 run 腳本手動往 manifest 塞了 anchoring。)
#   所以這裡用 target_factory 把 plan 的 hero edit 對回來，補上 anchoring / n_masters。
plan_attrs = {}
for e in hero_edits:
    t = e.get("target_factory") or e.get("target") or e.get("factory")
    if not t:
        continue
    d = plan_attrs.setdefault(t, {})
    a = e.get("anchoring")
    if a in pm.ANCHORINGS and "anchoring" not in d:
        d["anchoring"] = a
    try:
        n = int(e["n_masters"])
    except (KeyError, TypeError, ValueError):
        n = None
    if n is not None and n >= 0 and "n_masters" not in d:
        d["n_masters"] = n
    try:
        motion = wm.validate_runtime_request(e.get("world_motion"))
    except ValueError as error:
        raise ValueError(f"{t}: invalid world_motion: {error}") from error
    try:
        wm.validate_execution_context(
            motion, action=e.get("op"), anchoring=e.get("anchoring"))
    except ValueError as error:
        raise ValueError(f"{t}: invalid world_motion context: {error}") from error
    if motion["preset"] != "none" and "world_motion" not in d:
        d["world_motion"] = motion
    local = la.normalize_request(e.get("local_animation"), factory=t.split("(", 1)[0])
    if local["policy"] != "none" and "local_animation" not in d:
        d["local_animation"] = local
    if e.get("op") == "add":
        try:
            d["placement_spec"] = plc.normalize_placement_spec(
                e.get("placement_spec"), count=int(e.get("n")),
                anchoring=e.get("anchoring"),
                factory=e.get("factory"),
                code_from=e.get("code_from"))
        except (TypeError, ValueError) as error:
            raise ValueError(f"{t}: invalid placement_spec: {error}") from error
if plan_attrs:
    print(f"[manifest] plan 帶下來的擺放屬性: "
          f"{ {k: v for k, v in plan_attrs.items()} }", flush=True)


def rows_by_name_preview(roots, visible_rows_by_name):
    """只用來印訊息：這批 root 裡有幾個通過相機裁切。"""
    return [r for r in roots if r.name in (visible_rows_by_name or {})]


def placeholder_population_manifest_rows(
        coll, classname, seed, roots, *,
        n_added, code_from, visible_rows_by_name, cameras):
    claimed = placeholder_collection_claimed_by_plan(
        {"hero_edits": hero_edits},
        collection=coll.name,
        factory=classname,
        seed=seed,
    )
    # ★explicit 把三件事綁在一起，但它們的必要性不同★(2026-09-08)
    #   n_added>0  ── composer 這次★新建★的 placeholder。必須全生：它們是交付物，
    #                 而且剛建出來根本不在裁切表裡(force_... 的 docstring 講的就是
    #                 這個情形)。
    #   code_from / claimed ── ★replace 目標★。這裡原本也一律全生，於是 685 棵樹
    #                 全部 populate，而相機裁切算出來只有 110 棵看得到。
    #                 但裁切的定義是「在★任何一格、任何相機★都看不到」
    #                 (split_in_view.compute_inview_distances 逐格掃 frame_start..
    #                  frame_end，docstring: "any frame of any camera")，
    #                 被裁掉的樹本來就不會進畫面 ⇒ 補生它們對畫面沒有貢獻。
    #                 官方那張 confirm_raised.png 只 populate 26/685，畫面照樣是
    #                 完整的森林、沒有任何 placeholder 盒子露出來。
    #   ⇒ replace 目標也吃裁切。★2026-09-08 驗證後翻成預設★，三層證據：
    #     ・裁切集合 vs 官方 filter_populate_targets：40/40、70/70，
    #       ★漏 0 個、多 0 個★(而且 bound_box 讀到的是真實 4.65x4.65x4.00，
    #       不是 placement_modes 註解警告的預設單位立方 —— 那個坑只在
    #       ★重新開啟的 blend★才會踩到，apply_plan 是同一個 session 內算的)
    #     ・0908_flower 的相機 72 格完全靜止 ⇒ 沒有「後面幾格才露出來」
    #     ・frame-1 畫面比對：差異 >128 的像素只有 0.02%，無空洞
    #     樹 685→110，placer 917s→635s(-31%)，step 8-12 -20%。
    #   PLACEMENT_CULL_REPLACE_TARGETS=0 可退回舊的「replace 目標全生」。
    # ★2026-09-12 翻回不裁切★：replace 生出來的是★我們的資產★(藍色發光樹不是
    #   infinigen 的樹)，算我們加的東西；而且畫面豐富沒有不好。
    #   09-08 翻成裁切時的證據(補生對畫面沒貢獻、差異 0.02% 像素)是★在 dist_cull=200
    #   底下量的★ —— 那時被裁掉的確實都在畫面外。現在場景原生佈景降到官方 70m，
    #   同一條裁切會連 70-200m ★畫面內★的一起砍掉，那個證據就不適用了。
    #   PLACEMENT_CULL_REPLACE_TARGETS=1 可以開回裁切。
    cull_replace_targets = (
        os.environ.get("PLACEMENT_CULL_REPLACE_TARGETS", "1") != "0")
    explicit_added = n_added > 0
    explicit_claimed = bool(code_from or claimed)
    if explicit_claimed and cull_replace_targets and not explicit_added:
        print(f"[manifest] {coll.name}: replace 目標改吃相機裁切 "
              f"({len(rows_by_name_preview(roots, visible_rows_by_name))}/{len(roots)} 可見)",
              flush=True)
    rows_by_name = {
        root.name: dict(visible_rows_by_name[root.name])
        for root in roots
        if root.name in visible_rows_by_name
    }
    # ★放行誰★(2026-09-12 收緊)：以前 explicit 是★整個 collection★的開關 ——
    #   往既有的 TreeFactory collection 加一棵樹，476 棵全部繞過裁切，
    #   官方 70m 的對齊就白做了。現在只放行★真的被這個 plan 加進去的 root★。
    #   (replace 目標不在此列：它們吃裁切，跟場景原生佈景一樣 70m。)
    added_name_set = set(added_names)
    if explicit_claimed and not cull_replace_targets:
        forced_roots = roots                     # 舊行為：replace 目標全生
    elif explicit_added:
        forced_roots = [r for r in roots if r.name in added_name_set]
    else:
        forced_roots = []
    explicit = bool(forced_roots)
    if explicit:
        rows_by_name = php.force_population_target_metadata(
            forced_roots,
            cameras,
            rows_by_name,
        )
        for root in forced_roots:
            if root.name in rows_by_name:
                continue
            rows_by_name[root.name] = {
                "object": root.name,
                "will_populate": True,
                "coarse_cull_visible": False,
                "dist_m": None,
                "vis_dist_m": None,
                "distance_source": "explicit_placeholder_fallback",
            }
    ordered_names = [root.name for root in roots]
    targets = php.row_public_population_targets(rows_by_name, ordered_names)
    population_target_count = sum(
        1 for row in targets
        if row.get("will_populate", True)
    )
    cull_visible = sum(
        1 for row in targets
        if row.get("coarse_cull_visible", row.get("will_populate", True))
    )
    intent = classify_placeholder_population_intent(
        realized_at="populate",
        added_this_plan=n_added,
        code_from=code_from,
        will_populate_count=population_target_count,
        has_population_prediction=True,
    )
    if not DRY_RUN:
        target_by_name = {row["object"]: row for row in targets}
        for root in roots:
            row = target_by_name.get(root.name)
            will_materialize = bool(row and row.get("will_populate", True))
            root["bi_population_intent"] = intent
            root["bi_will_materialize"] = will_materialize
            if will_materialize and row.get("dist_m") is not None:
                root["bi_populate_dist_m"] = float(row["dist_m"])
            if will_materialize and row.get("vis_dist_m") is not None:
                root["bi_populate_vis_dist_m"] = float(row["vis_dist_m"])
    return {
        "claimed_by_composer": bool(claimed),
        "population_intent": intent,
        "population_target_count": population_target_count,
        "coarse_cull_visible_count": cull_visible,
        "population_targets": targets,
    }

placeholder_infos = []
for coll in sorted(bpy.data.collections, key=lambda c: c.name):
    m = PH_COLL_RE.fullmatch(coll.name)
    if not m:
        continue
    _full, classname, seed = m.groups()
    roots = roots_of(coll)
    if not roots:
        continue
    placeholder_infos.append({
        "collection": coll,
        "classname": classname,
        "seed": seed,
        "roots": roots,
    })

population_cameras = [o for o in bpy.data.objects if o.type == "CAMERA"]

# ── populate 的距離門檻 ★2026-09-12 對齊官方★ ───────────────────────────
#   官方 base_nature.gin:3  placement.populate_all.dist_cull = 70
#   我們以前 fallback 200 ⇒ 70-200m 的佔位在 baseline 是佔位、在我們這裡長成
#   真的東西。相機沒動，背景卻多一排樹(coast/seed2 TreeFactory 2/271 → 114/271)。
#   ★那個 200 是抄漏不是決定★：placement_modes 的 POPULATE_CULL 註解寫著照抄
#   generate_nature.py:856-935，但官方的 dist_cull 不在那段程式碼裡、在 gin 檔，
#   所以只抄到 per-factory 的 vis_cull(表裡 Cached* 兩筆寫 70 就是知道的人補的)。
#   代價實測：placer 佔整個 case 79% 的時間(coast 3446s/4367s)，絕大多數花在
#   長我們沒碰的背景。
#
#   ★但我們自己動過的東西要看得遠★：replace 目標是吃裁切的
#   (PLACEMENT_CULL_REPLACE_TARGETS 預設開)，一起降到 70 的話
#   canyon 的藍色發光樹會從 180/476 掉到 44/476，而且消失的 136 棵
#   ★不是變回原色，是整棵不渲染★。所以只有「我們沒碰的場景原生佈景」對齊官方。
#   ★replace 目標也用 70★(2026-09-12 想清楚後拿掉例外)：baseline 也是 70，
#   所以 @70 我們的畫面★跟 baseline 一樣密★，只是換了顏色 —— 不是變空。
#   canyon: baseline 44 棵原色 / 我們 44 棵藍色。留 200 的話是 180 棵藍色，
#   差異就變成「顏色 ＋ 136 棵樹的存在與否」，而後者不是我們改的東西。
#   ★唯一的例外是 hero add★：指定「放 10 隻企鵝」是數量契約，被距離砍掉等於違約。
#   那條在下面 force_population 處理，不在這裡。
#   ★replace 目標放寬到 200★：那是我們換上去的資產(藍色發光樹不是 infinigen 的樹)，
#   遠一點也該看得到 —— canyon 藍樹 44 → 180 棵。不是全生：仍然吃視野裁切，
#   畫面外的不會長。add 不走這裡，它在下面 force_population 一律全生。
OFFICIAL_DIST_CULL_M = 70     # base_nature.gin:3 placement.populate_all.dist_cull
REPLACED_DIST_CULL_M = 200    # 我們換上去的資產

population_visible_rows_by_name = {}
population_groups = {}
for info in placeholder_infos:
    cull = pm.populate_cull_for(info["classname"])
    replaced = (collection_code_intent.get(info["collection"].name)
                or collection_code_intent.get(
                    f"{info['classname']}({info['seed']})"))
    key = (cull.get("dist_cull",
                    REPLACED_DIST_CULL_M if replaced else OFFICIAL_DIST_CULL_M),
           cull.get("vis_cull", 0))
    population_groups.setdefault(key, []).extend(info["roots"])
for (dist_cull, vis_cull), roots in sorted(
        population_groups.items(), key=lambda item: str(item[0])):
    try:
        rows = php.population_target_metadata(
            roots,
            population_cameras,
            dist_cull=dist_cull,
            vis_cull=vis_cull,
            include_all=False,
        )
    except Exception as error:
        print(
            f"[manifest] ⚠️ placeholder population cull failed for "
            f"dist_cull={dist_cull!r}, vis_cull={vis_cull!r}: "
            f"{type(error).__name__}: {error}",
            flush=True,
        )
        rows = {}
    population_visible_rows_by_name.update(rows)
print(
    f"[manifest] coarse population cull: "
    f"{len(population_visible_rows_by_name)} visible target(s) across "
    f"{sum(len(info['roots']) for info in placeholder_infos)} placeholder root(s)",
    flush=True,
)

placements = []
for info in placeholder_infos:
    coll = info["collection"]
    classname = info["classname"]
    seed = info["seed"]
    roots = info["roots"]
    n_in_view = sum(1 for o in roots if o.name in INVIEW)
    n_added = sum(1 for o in roots if o.name in added_names)
    p = {
        "factory": classname,
        "collection": coll.name,
        "seed": int(seed),
        "count": len(roots),
        "in_view": n_in_view,
        "added_this_plan": n_added,
        # ★階段 B 把 generator 產物的路徑填進來★
        "code": None,
        # ⚠️ ★不能留空★ —— placer 會退到 populate_collection 的簽章預設 vis_cull=0，
        #    而 0 會把★全部★box 篩成「不在視野內」(2026-08-04 實測 231→0，改 4 則→44，
        #    正好對上 0729 那次成功的數字)。log 只寫「Populating 0 placeholders」不報錯。
        #    值照抄官方 generate_nature.py 的每個 factory 設定，見 placement_modes。
        "populate_kwargs": pm.populate_cull_for(classname),
    }
    # ★貼地方式★：階段 C 要據此在 populate 後用真幾何重算沉降。
    #   真正權威的是每個 box 自己的 bi_anchoring(同一個 collection 可能混不同來源)，
    #   這裡只是把它攤在 manifest 上讓人看得到。
    _anchors = sorted({o["bi_anchoring"] for o in roots if "bi_anchoring" in o})
    p["anchoring"] = _anchors[0] if len(_anchors) == 1 else (_anchors or None)
    # box 沒被 stamp 過(★replace 路徑★)就退到 plan 針對這個 factory 給的值。
    # ★box 優先★ —— 同一個 collection 可能混不同來源，box 自己身上的才是權威。
    # ⚠️ ★三種寫法都要認★(2026-08-05 端到端抓到)：director 的 target_factory 用的是
    #    manifest 上的名字 `TreeFactory(3040046)`，這裡的 classname 只有 `TreeFactory`。
    #    先前只用 classname 查 ⇒ 永遠查不到 ⇒ anchoring 靜默維持 None，
    #    等於「修了 replace 的 anchoring」這件事本身沒生效。
    _attrs = (plan_attrs.get(f"{classname}({seed})")
              or plan_attrs.get(coll.name)
              or official_scatter_attrs.get(coll.name)
              or plan_attrs.get(classname)
              or {})
    if p["anchoring"] is None and _attrs.get("anchoring"):
        p["anchoring"] = _attrs["anchoring"]
        p["anchoring_from"] = "plan"
    elif p["anchoring"] is not None:
        p["anchoring_from"] = "box(bi_anchoring)"
    # 母本池大小：director 逐筆決定(森林要多樣給大值、幾隻動物給小值)。
    # 沒給就留 None，讓 placer 用它的預設(只對 generated 資產生效)。
    if _attrs.get("n_masters") is not None:
        p["n_masters"] = _attrs["n_masters"]
    if _attrs.get("world_motion") is not None:
        p["world_motion"] = _attrs["world_motion"]
    if _attrs.get("local_animation") is not None:
        p["local_animation"] = _attrs["local_animation"]
    if _attrs.get("placement_spec") is not None:
        p["placement_spec"] = _attrs["placement_spec"]
    _code_from = (collection_code_intent.get(coll.name)
                  or collection_code_intent.get(f"{classname}({seed})"))
    if _code_from:
        p["code_from"] = _code_from
    population_metadata = placeholder_population_manifest_rows(
        coll,
        classname,
        seed,
        roots,
        n_added=n_added,
        code_from=_code_from,
        visible_rows_by_name=population_visible_rows_by_name,
        cameras=population_cameras,
    )
    p.update(population_metadata)
    p["in_view"] = population_metadata["coarse_cull_visible_count"]
    # ★★代理佔位：這個 collection 裡有「其實要長成別的東西」的 box★★
    #   (2026-07-29 加)。box 借用了代理 factory，但★意圖是長成 wants★，
    #   ⇒ 階段 B 要為 wants 生 code，階段 C 把 code 填進來時就會長對東西。
    #   ⚠️ 不記的話，這個資訊★在階段 A 之後就永久遺失★。
    wants = sorted({proxy_intent[o.name] for o in roots if o.name in proxy_intent})
    if wants:
        p["proxy_for"] = wants
        p["note"] = (f"★這批 box 有 {sum(1 for o in roots if o.name in proxy_intent)} 個"
                     f"是代理佔位★，實際要長成 {', '.join(wants)}。"
                     f"階段 B 請為它生 code，階段 C 填進 code 欄。")
    # ★這批東西在哪個階段被實現、因此該用什麼手段編輯★
    #   走到這裡的都是 placeholders: 且 roots 非空 ⇒ 還沒長出來 ⇒ 歸 populate。
    p["realized_at"] = "populate"
    p["edit_via"] = "placeholder"
    placements.append(p)

# ════════════════════════════════════════════════════════
# ★★coarse 階段就已實現的東西★★(2026-08-05 加)
#   上面的迴圈只掃 `placeholders:` 且 roots 非空的 collection ⇒ swarm 這類
#   「placeholder 已被消耗成 0 個」的東西★根本不會出現在 manifest 上★，
#   於是「這個場景有魚群」這件事在階段 A 之後就消失了，director 也無從得知。
#
#   判定全部是掃 collection 結構得到的★確定性資訊★，不需要 call API：
#     placeholders:X 空 + unique_assets:X 有東西  ⇒ coarse 就 populate 掉了
#     particleassets:X.children 存在              ⇒ 群內個體是 boid 粒子，
#                                                   換掉那個 collection 的母本即可
#   (實例：fish_school / bug_swarm，見 0805.txt 十一節的全場盤點)
_ph_names = {c.name.split(":", 1)[1] for c in bpy.data.collections
             if c.name.startswith("placeholders:") and len(c.objects) > 0}
for c in sorted(bpy.data.collections, key=lambda c: c.name):
    if not c.name.startswith("unique_assets:") or not len(c.objects):
        continue
    key = c.name.split(":", 1)[1]
    if key in _ph_names:
        continue                    # 還有 box 等著長 ⇒ 上面的護欄已經擋掉了
    m = re.fullmatch(r"([A-Za-z_0-9]+)\((\d+)\)", key)
    part = next((pc for pc in bpy.data.collections
                 if pc.name == f"particleassets:{key}.children"), None)
    placements.append({
        "factory": m.group(1) if m else key,
        "collection": c.name,
        "seed": int(m.group(2)) if m else None,
        "count": len(c.objects),
        "in_view": None,
        "added_this_plan": 0,
        "code": None,
        # replace 的魚群：fillcode 依 code_from 填 code ⇒ placer 走 master_swap
        # ★同一個 factory 的每一群都要換★(2026-09-13 使用者：小丑魚沒有取代魚群)。
        #   coral_reef 有三群 FishSchoolFactory，composer 只對其中一群下 replace ⇒
        #   畫面上兩群舊魚、一群新魚，看起來像沒換。跟「所有 scatter 載體都要改」同理。
        "code_from": (_swarm_replace_for(key) or {}).get("code_from"),
        "n_masters": (_swarm_replace_for(key) or {}).get("n_masters"),
        "populate_kwargs": None,
        "anchoring": None,
        "realized_at": "coarse",
        "edit_via": "particle_col" if part else "master_swap",
        "master_collection": part.name if part else None,
        "n_masters_existing": len(part.objects) if part else None,
        "note": ("★coarse 階段就 populate 掉了★，placeholder 已被消耗。"
                 "不能用 replace/add(hero) 編輯 —— 要換的是"
                 f"{'粒子系統的 instance collection' if part else '母本'}裡的東西。"),
    })
    print(f"[manifest] ℹ️  {c.name}: realized_at=coarse "
          f"edit_via={'particle_col' if part else 'master_swap'}"
          f"{f' 母本 {len(part.objects)} 個' if part else ''}", flush=True)

manifest = {
    "coarse_blend": OUT_BLEND if not DRY_RUN else IN_BLEND,
    "source_blend": IN_BLEND,
    "plan": PLAN_JSON,
    "note": ("階段 A 的產物。code 欄位由 asset generator(階段 B) 填入 overlay 目錄路徑，"
             "然後交給 placer.py(階段 C) 執行 populate。"
             "不填 code 就用官方 factory。"),
    "placements": placements,
    "explicit_code_routing": True,
}
MANIFEST_OUT = str(Path(REPORT_JSON).with_name(
    Path(REPORT_JSON).stem.replace("report", "manifest") + ".json"))
Path(MANIFEST_OUT).parent.mkdir(parents=True, exist_ok=True)
Path(MANIFEST_OUT).write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8")
report["manifest_out"] = MANIFEST_OUT
print(f"[plan] ★placement manifest★ → {MANIFEST_OUT} ({len(placements)} 筆)", flush=True)

if not DRY_RUN:
    print(f"[plan] saving → {OUT_BLEND}", flush=True)
    Path(OUT_BLEND).parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=OUT_BLEND, compress=False)

visibility_contract_failures = [
    {
        "index": row.get("index"),
        "target": row.get("target"),
        "reason": row.get("blocked_reason"),
    }
    for row in report["extra_results"]
    if row.get("visibility_guarantee_passed") is False
]
report["scatter_visibility_contract_failures"] = visibility_contract_failures

Path(REPORT_JSON).parent.mkdir(parents=True, exist_ok=True)
Path(REPORT_JSON).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
print(f"[plan] report → {REPORT_JSON}", flush=True)
print(f"[plan] ★needs_populate = {report['needs_populate']}★"
      f"{' (~10 分鐘)' if report['needs_populate'] else ' (可直接 render)'}", flush=True)

if visibility_contract_failures:
    raise SystemExit(3)
print("PLAN_DONE", flush=True)
