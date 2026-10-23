"""擺放模式 —— anchoring / scatter_region 的唯一定義，以及地面貼合的幾何。

★這份是唯一來源★：director 產出欄位、生成器寫進 factory.txt、apply_plan 與 composer
擺放時，全部 import 這裡。同一張表散在多處、改了一處忘另一處，是這個 repo 已經踩過
兩次的坑(0730 3-A 的 .env 路徑、0731 3-B 的 blender generator)。

幾何部分只在 Blender(bpy)裡跑；表格與純資料的部分不 import bpy，director 之類的
非 Blender 程式也能用。

對照 infinigen：
  normal_fac  = core/placement/instance_scatter.py 的 AlignEulerToVector 插值強度
                (官方植物實際用 0.3、水母 0.0)
  altitude    = core/placement/placement.py 的高度偏移
                (官方樹 -0.1 微陷、魚群 +2 懸浮)
infinigen 的 placeholder 一律 `Euler((0, 0, rot_z))`(placement.py:124) —— 它自己
不做坡面對齊，因為它生成的東西以樹木植被為主。我們是在既有場景上編輯、而且要放
車輛這類長條物體，所以才需要把 scatter 那套法線對齊搬到 placeholder 這條線上。
"""
from __future__ import annotations
import re

# ── anchoring：hero 物體怎麼「靠」在它所在的表面上 ──────────────────────────
#   ★這是物體自己的性質，跟場景無關★ —— 摩托車在森林、沙漠、屋頂都是 conform。
#   (normal_fac, altitude, 說明)
ANCHORINGS = {
    "upright":   (0.0, -0.10, "自身軸垂直、要重新落地。★只給窄底面(<0.5m)★：路燈、標誌、人"),
    "rooted":    (0.0,  0.00, "原地不動：本來就長在那裡，換的是同類(樹換樹)"),
    "tilted":    (0.3, -0.05, "大致貼地但隨坡度微傾(灌木、石頭、蘑菇)"),
    "conform":   (1.0,  0.00, "順著坡面。★接地點跨距 >1m 的一律用這個★：車輛、長椅、倒木、★四足動物★"),
    "afloat":    (0.0, -0.15, "浮在液面上的船艇；保持直立並讓船體略微吃水，不可套 submerged 的懸浮高度"),
    "airborne":  (0.0, +3.00, "不接觸地面，懸在上方(飛鳥、蟲群、雲)"),
    "submerged": (0.0, +2.00, "在水體之內(魚、水母) —— 需搭配 underwater 地形選取"),
}
# 最保守 = ★原地不動★，等同這支模組出現之前的行為。2026-08-05 前這個角色由 upright
# 兼任，拆出 rooted 之後預設值跟著搬過來，否則「沒指定 anchoring」會突然開始移動物體。
DEFAULT_ANCHORING = "rooted"

# ═══════════════════════════════ SA1 ablation：拿掉貼地語彙(2026-09-20)
# BI_ABLATION_NO_ANCHORING=1 ⇒ 退回 ★infinigen 官方 placeholder 行為★：
#   placement.py:124 一律 Euler((0,0,rot_z))、沒有法線對齊、沒有逐物件沉降，
#   高度只有 scatter 呼叫共用的一個 scalar altitude。
#   (官方的法線對齊只存在於 instance_scatter：植物 0.3、水母 0.0)
# ★這張表是唯一來源★ —— director / composer / apply_plan / placer 全部 import
#   它，所以一處歸零就一致地傳到所有消費端。
# ⚠️ 這★不會★讓魚掉到海床：submerged/airborne 的錨點是相機射線上的 3D 點，
#    altitude 只是偏移。要那個效果請用 BI_ABLATION_NO_VOLUME_PLACEMENT(見
#    placement_region_contract.py)。
import os as _os  # noqa: E402

if _os.environ.get("BI_ABLATION_NO_ANCHORING") == "1":
    ANCHORINGS = {k: (0.0, 0.0, v[2]) for k, v in ANCHORINGS.items()}

# 不接觸地面的模式：跳過法線估計與沉降，只做高度偏移。
AIRBORNE_MODES = {"airborne", "submerged"}
TERRAIN_NAME_PREFIXES = ("OpaqueTerrain", "CollectiveTransparentTerrain")

# ★不重新落地的模式★：位置沿用呼叫端給的，只套 yaw。
#   為什麼需要這個豁免：沉降是「把底部接觸點抬到不穿地」，但★樹的最低點不是接地點★
#   —— 下垂的枝葉會低於樹幹基部，contact_gap 的 5×5 網格每一格取到的也還是枝葉。
#   拿它去貼地會把整棵樹往上推。實測：糖果樹 462 棵中位數 +2.15m、最大 +4.76m，肉眼可見飄起來。
#
#   ⚠️ ★2026-08-05 修正：這個豁免原本掛在 "upright" 上，掛錯了條件★
#      upright 講的是「姿態」(自身軸垂直)，需要豁免的卻是「原地性」(本來就長在這、換的是同類)。
#      樹剛好兩者皆是，站立的動物只有前者 —— 於是長頸鹿被誤傷，直接繼承被替換的樹的 z。
#      實測 20 隻 upright 長頸鹿：高度修正 0.00m、接地間隙 -0.99~+0.11、★5/20 偏離>0.3m★；
#      同樣 20 隻改走沉降(tilted)：間隙全部精確 -0.05(= altitude 常數)、0/20。
#      ⇒ 豁免改掛在 "rooted" 上，upright 恢復字面語意並照常沉降。
NO_RESETTLE_MODES = {"rooted"}
if _os.environ.get("BI_ABLATION_NO_ANCHORING") == "1":
    # 官方不做逐物件沉降 ⇒ 全部模式都不重新落地
    NO_RESETTLE_MODES = set(ANCHORINGS)
    print("[ablation] ★BI_ABLATION_NO_ANCHORING=1★ anchoring 表歸零 + 全部不沉降",
          flush=True)

# ── scatter_region：grime / ground_scatter 撒在表面的哪一塊 ────────────────
#   grime 與 ground_scatter 是同一套機制(scatter_instances)，差別只在 base_obj
#   傳地形還是傳某個資產物件，所以共用同一份詞彙、沒有 anchoring。
SCATTER_REGIONS = {
    "land":       "地形的陸地部分(草、蕨類、蘑菇)",
    "underwater": "地形的水下部分(海草、珊瑚、貝殼)",
    "lower":      "宿主物件的下半部(苔蘚爬樹幹 —— 官方 scatter_lower)",
    "upward":     "宿主物件朝上的面(地衣長在石頭頂 —— 官方 scatter_upward)",
    "all":        "整個表面不篩選(藤蔓爬滿整棵樹)",
}
DEFAULT_SCATTER_REGION = {"ground_scatter": "land", "grime": "all"}

# 擬合出來的坡度上限。超過就往垂直方向收 —— 真實地形不會讓一台車站在 60° 崖面上，
# 出現這種值幾乎都是取樣退化(見 support_normal 的共線判斷)。
MAX_TILT_DEG = 45.0

# ── populate 的視野裁切參數 ────────────────────────────────────────────────
#   ★照抄 infinigen_examples/generate_nature.py:856-935★(每個 factory 各自不同)。
#   vis_cull = 允許落在視錐外多遠仍然要 populate(公尺)；dist_cull = 離相機最遠多少。
#   ⚠️ ★vis_cull 不能用 0★ —— 2026-08-04 實測 0 會把★全部★box 篩掉
#      (231 個 → 0 個，換成 1 就有 46 個)，而 populate_all 的函式簽章預設正好是 0。
#      apply_plan 以前寫 populate_kwargs:{} ⇒ placer 退到 0 ⇒ 整場一個都長不出來，
#      而且 log 只寫「Populating 0 placeholders」不報錯 —— 又一個靜默失敗。
POPULATE_CULL = {
    "TreeFactory":        {"vis_cull": 4},
    "BushFactory":        {"vis_cull": 1},
    "BoulderFactory":     {"vis_cull": 3},
    "KelpMonocotFactory": {"vis_cull": 5},
    "CactusFactory":      {"vis_cull": 6},
    "CloudFactory":        {"vis_cull": None, "dist_cull": None},
    "GlowingRocksFactory": {"vis_cull": None, "dist_cull": None},
    "CachedTreeFactory":    {"vis_cull": 4, "dist_cull": 70},
    "CachedBoulderFactory": {"vis_cull": 3, "dist_cull": 70},
}
DEFAULT_POPULATE_CULL = {"vis_cull": 4}    # 沒列到的 factory：用樹的值，不要用 0
# ⚠️ ★這張表只有 vis_cull 是照抄來的★。官方的 dist_cull 不在 generate_nature.py 裡，
#    在 configs_nature/base_nature.gin:3 → placement.populate_all.dist_cull = 70。
#    (上面 Cached* 兩筆寫 70 就是知道這件事的人補的；其餘沒寫 ⇒ 以前滾到 apply_plan
#     的 fallback 200，等於看得比官方遠三倍。2026-09-12 已在 apply_plan 對齊成 70，
#     ★replace 目標也一樣★ —— baseline 也是 70，所以 @70 我們的畫面跟 baseline
#     一樣密、只是換了顏色(canyon 44 棵原色 vs 44 棵藍色，不是 44 vs 180)。
#     ★唯一例外是 hero add★：那是數量契約，不吃裁切。)


# ═══════════════════════════════════════════ 官方 factory 的母本池(2026-09-09)
#
# ★為什麼原本不開★：master_pool 的模組註解寫著「不要對官方 factory 無差別開啟，
#   一片森林 231 棵樹只剩 5 種，會毀掉 infinigen 本來的多樣性」。那是對的 ——
#   ★但那句話的前提是 DEFAULT_N_MASTERS=5★。
#
# ★為什麼現在開★：實測水下場景的官方 KelpMonocotFactory 248 株 ★34.5 秒/株 =
#   8557 秒(143 分)★，而那 8557 秒對畫面沒有任何貢獻(海帶是場景本來就有的東西，
#   不是我們加的)。n=20 時只真建 20 株、其餘 linked duplicate ⇒ ★11.5 分(12x)★，
#   而 248 株裡有 20 種變化，重複度遠低於 n=5 的情形。
#
# ★逐 factory 而不是全域開關★：重複看不看得出來，取決於那個資產在畫面上多大、
#   形狀多有辨識度。這是視覺判斷，寫成表格才能一支一支調、一支一支驗。
#   ・OFFICIAL_N_MASTERS 是預設；OFFICIAL_POOL 逐支覆寫；0 = 不開池
#   ・BI_OFFICIAL_POOL=0 可以整個關掉(回到 9/08 以前的行為)
#
# ⚠️ 已知會自動關池的情形(master_pool 內已處理，不必在這裡列)：
#     ・帶 local_animation policy="inherit" 的資產(armature pose 無法安全複製)
#     ・create_asset 回傳 placeholder 本人的 factory
OFFICIAL_N_MASTERS = 20
OFFICIAL_POOL = {
    # 佔畫面大、樹冠形狀辨識度高 —— 重複很明顯，維持逐棵生成
    "TreeFactory": 0,
    "BushFactory": 0,
}


def official_pool_size(factory: str) -> int:
    """官方 factory 的母本池大小(0 = 不開池，逐個生成)。"""
    import os
    if os.environ.get("BI_OFFICIAL_POOL", "1") == "0":
        return 0
    return int(OFFICIAL_POOL.get(str(factory or ""), OFFICIAL_N_MASTERS))


def populate_cull_for(factory: str) -> dict:
    """factory 類名 -> populate 的 cull 參數(照抄官方，沒列到就用保守預設)。"""
    return dict(POPULATE_CULL.get(factory, DEFAULT_POPULATE_CULL))


def params_for(anchoring: str | None) -> tuple[float, float]:
    """anchoring -> (normal_fac, altitude)。給無效值就退回預設，不猜。"""
    return ANCHORINGS.get(anchoring or DEFAULT_ANCHORING,
                          ANCHORINGS[DEFAULT_ANCHORING])[:2]


# ═══════════════════════════════════════════════ 以下需要 bpy(只在 Blender 裡跑)
def _bpy():
    import bpy
    import mathutils
    return bpy, mathutils


def local_matrix(o):
    """★只用權威屬性★組出 local 矩陣(不讀 matrix_local / matrix_basis)。

    ⚠️ 2026-08-04 的教訓：`bound_box`、`matrix_local`、`evaluated_get().matrix_world`
       在★重新開啟的 blend★裡都是沒重算的導出快取(讀到 0 或預設單位立方)，連續四版
       量測全部栽在上面，還據此改錯了程式。權威的只有 location / rotation_* / scale
       與 mesh.vertices[].co。
    """
    _, mathutils = _bpy()
    Matrix, Vector = mathutils.Matrix, mathutils.Vector
    if o.rotation_mode == "QUATERNION":
        rot = o.rotation_quaternion.to_matrix().to_4x4()
    elif o.rotation_mode == "AXIS_ANGLE":
        aa = o.rotation_axis_angle
        rot = Matrix.Rotation(aa[0], 4, Vector(aa[1:]).normalized())
    else:
        rot = o.rotation_euler.to_matrix().to_4x4()
    basis = (Matrix.Translation(o.location) @ rot
             @ Matrix.Diagonal(Vector(o.scale).to_4d()))
    # delta_* 是獨立於 loc/rot/scale 的第二層變換(matrix_basis 不含它)。infinigen 沒在
    # 用，但漏掉會靜默算錯，補上很便宜。
    delta = (Matrix.Translation(o.delta_location)
             @ o.delta_rotation_euler.to_matrix().to_4x4()
             @ Matrix.Diagonal(Vector(o.delta_scale).to_4d()))
    return delta @ basis


def authoritative_world(o):
    """從 parent 鏈用權威屬性乘出世界矩陣。

    infinigen 的 populate 用 `parent_to(..., no_inverse=True)` ⇒ parent inverse 是
    單位矩陣，所以直接連乘 local 矩陣就對。真的碰到非單位的 parent inverse 時把它
    乘進去(不忽略，寧可慢也不要默默算錯)。
    """
    _, mathutils = _bpy()
    M = mathutils.Matrix.Identity(4)
    chain = []
    cur = o
    while cur is not None:
        chain.append(cur)
        cur = cur.parent
    for node in reversed(chain):
        M = M @ (node.matrix_parent_inverse if node.parent is not None
                 else mathutils.Matrix.Identity(4)) @ local_matrix(node)
    return M


def world_points(obj, recompute=True):
    """整個階層(含 children)所有 mesh 頂點的世界座標 -> numpy (N,3)，沒有就 None。

    `obj` 可以給★單一物件★或★物件清單★(要量「只有資產、不含 placeholder box」
    這種子集合時用；見 place() 的 bbox_objs)。

    ★預設 recompute=True：自己用權威屬性把矩陣乘出來，不讀 matrix_world。★
      ・離線讀存好的 blend：matrix_world 是舊快取，實測 spawn_asset 全部讀到 0，
        20 台量出一模一樣的 -4.00m(「數值完全相同」就是量測壞掉的訊號)。
      ・★連 session 內部也不可信★(2026-08-05 實測)：place() 第③步已經先做過
        view_layer.update() 才量，量到的 asset 位置仍比真實低約 0.27m，
        於是 `-gap` 抬過頭 ⇒ 20 台全部浮空(中位數 +0.27m，近拍看得到影子分離)。
      ⇒ 不要再為了省時間切回 matrix_world；權威路徑的成本跟它同一個量級
        (兩者都是每個頂點做一次矩陣乘法)。
    `recompute=False` 只留給確定 depsgraph 是乾淨的場合。
    """
    import numpy as np
    roots = list(obj) if isinstance(obj, (list, tuple, set)) else [obj]
    pts = []
    stack = [(o, authoritative_world(o) if recompute else None) for o in roots]
    while stack:
        o, M = stack.pop()
        m = M if M is not None else o.matrix_world
        for ch in o.children:
            stack.append((ch, (M @ ch.matrix_parent_inverse @ local_matrix(ch))
                          if M is not None else None))
        if o.type == "MESH" and len(o.data.vertices):
            pts += [(m @ v.co)[:] for v in o.data.vertices]
    return np.array(pts) if pts else None


def world_bbox(obj, recompute=True):
    """整個階層(含 children)的世界座標 bounding box -> (min_xyz, max_xyz)。

    root 常常是 Empty(dimensions 全 0)、幾何掛在 children 上，所以不能只看 obj。
    """
    a = world_points(obj, recompute=recompute)
    if a is None:
        return None, None
    return a.min(0), a.max(0)


def _rank_terrain_objects(objects, deps):
    _, mathutils = _bpy()
    Vector = mathutils.Vector
    ranked = []
    for o in objects:
        if o.type != "MESH" or "collider" in o.name:
            continue
        ev = o.evaluated_get(deps)
        me = getattr(ev, "data", None)
        if me is None or len(me.vertices) == 0:
            continue
        bb = [ev.matrix_world @ Vector(c[:]) for c in ev.bound_box]
        xs = [p.x for p in bb]
        ys = [p.y for p in bb]
        area = (max(xs) - min(xs)) * (max(ys) - min(ys))
        ranked.append((o.hide_render, -area, o))
    ranked.sort(key=lambda t: (t[0], t[1]))
    return [o for _, _, o in ranked]


def terrain_candidates(name_prefix=TERRAIN_NAME_PREFIXES,
                       surface_names=None, surface_types=None):
    """場景裡可以拿來貼地的地形物件，依可靠度排序(最可靠在前)。

    ★不要寫死名字★ —— 0803 的 conform 測試就是栽在這裡：composer/apply_plan 都寫死
    優先 `OpaqueTerrain.inview_near`，但它是 LOD 分塊，實測只覆蓋 28x22 m，而
    placeholder 散佈在 150x150 m，30 台車有 28 台 ray_cast 直接打空(回傳垂直法線
    ⇒ 完全不轉，看起來像「機制沒生效」)。而且它 hide_render=True，根本不是畫面上
    看到的那個地面 —— 真正被渲染的是 OpaqueTerrain_fine。

    排序準則(通用，不綁特定 blend)：
      ① 會被渲染的優先(hide_render=False) —— 物體要貼的是「看得到的那個表面」
      ② 覆蓋範圍大的優先 —— LOD 分塊排在全域地形之後
    沒有 evaluated mesh 的(空殼、純容器)直接排除，collider 代理也排除。
    """
    import bpy as _b
    _, mathutils = _bpy()
    Vector = mathutils.Vector
    deps = _b.context.evaluated_depsgraph_get()
    prefixes = ((name_prefix,) if isinstance(name_prefix, str)
                else tuple(name_prefix or TERRAIN_NAME_PREFIXES))
    ranked = []
    for o in _b.data.objects:
        if o.type != "MESH" or not o.name.startswith(prefixes):
            continue
        if "collider" in o.name:
            continue                     # 物理代理，不是視覺表面
        ev = o.evaluated_get(deps)
        me = getattr(ev, "data", None)
        if me is None or len(me.vertices) == 0:
            continue                     # OpaqueTerrain 這種帶修改器的空殼
        bb = [ev.matrix_world @ Vector(c[:]) for c in ev.bound_box]
        xs = [p.x for p in bb]
        ys = [p.y for p in bb]
        area = (max(xs) - min(xs)) * (max(ys) - min(ys))
        ranked.append((o.hide_render, -area, o))
    ranked.sort(key=lambda t: (t[0], t[1]))
    if ranked:
        return [o for _, _, o in ranked]

    requested_names = {
        str(name) for name in (surface_names or []) if str(name)
    }
    requested_types = {
        str(value) for value in (surface_types or []) if str(value)
    }
    if not requested_names and not requested_types:
        return []

    fallback_objects = []
    if requested_names:
        for o in _b.data.objects:
            if o.type != "MESH" or "collider" in o.name:
                continue
            if o.name in requested_names:
                fallback_objects.append(o)

    if not fallback_objects and requested_types:
        try:
            import surface_registry as sr
        except Exception:
            sr = None
        patterns = set()
        if sr is not None:
            for scene_rules in sr.DATA.get("scenes", {}).values():
                for surface_type in requested_types:
                    patterns.update(scene_rules.get(surface_type, []))
        if patterns:
            for o in _b.data.objects:
                if o.type != "MESH" or "collider" in o.name:
                    continue
                if any(re.fullmatch(pattern, o.name, re.IGNORECASE)
                       for pattern in patterns):
                    fallback_objects.append(o)

    if not fallback_objects:
        return []
    return _rank_terrain_objects(fallback_objects, deps)


def prepare_terrains(terrain, recompute=True):
    """把地形物件整理成可以反覆 ray_cast 的形式 -> [(evaluated, mw, mw⁻¹, mw_3x3)]。

    `recompute=True` 時矩陣改用權威屬性乘出來(離線讀存好的 blend 用)；★幾何仍然要用
    evaluated 版本★ —— ray_cast 需要它，而網格資料本身不是快取。

    一定要用 evaluated 版本：blend 裡有些地形帶修改器，直接對原始物件 ray_cast 會拿到
    "has no evaluated mesh data"。matrix 也要跟著用 evaluated 的。
    (實測 OpaqueTerrain_fine 135 萬頂點、無修改器：首次 ray_cast 建 BVH 0.74s，之後
     每次幾乎免費，view_layer.update() 也不會讓它重算 ⇒ 不需要自己快取 BVH。)
    """
    import bpy as _b
    terrains = list(terrain) if isinstance(terrain, (list, tuple)) else [terrain]
    deps = _b.context.evaluated_depsgraph_get()
    out = []
    for t in terrains:
        if t is None:
            continue
        ev = t.evaluated_get(deps)
        mw = authoritative_world(t) if recompute else ev.matrix_world
        out.append((ev, mw, mw.inverted(), mw.to_3x3()))
    return out


def _down_hits(ev, inv, origin, max_hits=16):
    """Every surface of one terrain a straight-down ray crosses -> [(local hit, local normal)].

    ★洞穴/懸垂地形★(2026-09-10 cave)：從 z+60m 往下打，第一個命中是★洞頂外側★，
    資產被沉降到洞頂上(高 5m、出畫面)。所以收集沿途每一層，由呼叫端挑離參考高度
    最近的那層；露天地形只有一層，結果與以往相同。"""
    _, mathutils = _bpy()
    down = (inv.to_3x3() @ mathutils.Vector((0, 0, -1))).normalized()
    point = inv @ origin
    hits = []
    for _ in range(max_hits):
        ok, lh, ln, _ = ev.ray_cast(point, down)
        if not ok:
            break
        hits.append((lh, ln))
        point = lh + down * 1e-3
    return hits


def terrain_z_at(prepared, x, y, z_hint, up_offset=60.0):
    """(x, y) 正上方垂直打下去，回傳★真實地形表面★的世界 z；全部打空回 None。

    多個候選地形依序試，第一個打中的算數(LOD 分塊補不到的地方由全域地形接手)。
    同一地形有多層(洞穴)時取離 z_hint 最近的那層。
    """
    _, mathutils = _bpy()
    Vector = mathutils.Vector
    origin = Vector((x, y, z_hint + up_offset))
    for ev, mw, inv, _rot3 in prepared:
        try:
            hits = _down_hits(ev, inv, origin)
        except RuntimeError:
            continue
        if hits:
            return min((float((mw @ lh).z) for lh, _ln in hits),
                       key=lambda z: abs(z - z_hint))
    return None


def contact_gap(measure, prepared, grid_n=5, band_frac=0.30, band_min=0.15,
                recompute=True):
    """資產底面對★真實地形★的間隙 -> (gap, n_hit, n_pts)。

    gap > 0：整體懸空(最接近的接地點離地 gap)；gap < 0：最深的地方陷入 |gap|。
    要貼合就把物體抬 -gap。

    ★為什麼需要這個★(2026-08-04 遺留問題)：沉降原本拿 support_normal 回傳的
    ★最小平方擬合平面★當高度基準，但低模地形是硬折面，起伏大的地方擬合平面跟真實
    表面差到 0.5m ⇒ 20 台摩托車有 8 台陷進地裡 0.3~0.64m(輪徑才 0.6m，等於半顆輪子
    埋在土裡)。傾角用擬合平面是對的(要的就是一片的平均走向)，★高度不行★ —— 高度
    必須問真實表面。

    取樣方式：把資產底部 band(最低點往上 band_frac 的高度)的頂點丟進 grid_n×grid_n
    的 XY 網格，每格取最低的一點當「接地點」，逐點 ray_cast。
      ・只取底部 band —— 上半部(把手、油箱)不會接觸地面，把它們算進來的話，旁邊
        一道上升的土坡會被誤判成「陷入」，整台被抬到空中。
      ・取 min 而不是平均 —— 只要有★任何一點★陷進去就是穿模，物體應該停在最先
        碰到的那點上(等同無穿透的靜置)。
    """
    import numpy as np
    if not prepared:
        return None, 0, 0
    a = world_points(measure, recompute=recompute)
    if a is None or not len(a):
        return None, 0, 0
    zmin, zmax = float(a[:, 2].min()), float(a[:, 2].max())
    band = max(band_min, band_frac * (zmax - zmin))
    low = a[a[:, 2] <= zmin + band]
    if not len(low):
        low = a

    # XY 網格 -> 每格最低點
    x0, x1 = float(low[:, 0].min()), float(low[:, 0].max())
    y0, y1 = float(low[:, 1].min()), float(low[:, 1].max())
    ix = np.zeros(len(low), dtype=int) if x1 - x0 < 1e-9 else \
        np.minimum(((low[:, 0] - x0) / (x1 - x0) * grid_n).astype(int), grid_n - 1)
    iy = np.zeros(len(low), dtype=int) if y1 - y0 < 1e-9 else \
        np.minimum(((low[:, 1] - y0) / (y1 - y0) * grid_n).astype(int), grid_n - 1)
    cell_best = {}
    for k, (cx, cy) in enumerate(zip(ix, iy)):
        key = (int(cx), int(cy))
        if key not in cell_best or low[k, 2] < low[cell_best[key], 2]:
            cell_best[key] = k
    samples = low[list(cell_best.values())]

    gaps = []
    for px, py, pz in samples:
        gz = terrain_z_at(prepared, float(px), float(py), float(pz))
        if gz is not None:
            gaps.append(float(pz) - gz)
    if not gaps:
        return None, 0, len(samples)
    return min(gaps), len(gaps), len(samples)


def contact_spread(measure, prepared, k=4, foot_frac=0.12, recompute=True):
    """★逐支腳的離地高度★ -> (spread, gaps, n)。spread = 最高的腳 - 最低的腳。

    ⚠️ ★為什麼不能只看 contact_gap★(2026-08-05 傍晚踩到，使用者看圖抓的)：
       contact_gap 取的是所有接地點裡的★最小★間隙，只證明「沒穿地」，對「站不站得
       穩」零解析力。20 隻長頸鹿全部精確 -0.100 我當成誤差為零，實際上只代表
       ★至少一隻腳碰到地★ —— 另外三隻可以懸在半空。摩托車(normal_fac=1.0 四點齊平)
       剛好把這個瑕疵藏起來，所以從 0804 一路沒被抓到。

    做法：取物體★自身高度★最低 foot_frac 的點(= 蹄/輪胎，不是肚子/車架 —— 那些
    本來就該懸空，長頸鹿肚子離地 2m 是正常的)，依 XY 做 k-means 分成 k 群，
    每群最低點各自 ray_cast。

    實測(長頸鹿 ×20，80 隻腳)：normal_fac 0.0 → 19 隻腳 >0.3m；1.0 → 只剩 2 隻。
    """
    import numpy as np
    if not prepared:
        return None, [], 0
    a = world_points(measure, recompute=recompute)
    if a is None or len(a) < k * 8:
        return None, [], 0
    zmin, zmax = float(a[:, 2].min()), float(a[:, 2].max())
    foot = a[a[:, 2] <= zmin + foot_frac * (zmax - zmin)]
    if len(foot) < k * 3:
        return None, [], 0

    # k-means(XY)。固定網格會把一隻腳切兩半、或把兩隻併成一格，所以用分群。
    order = np.argsort(foot[:, 0] * 0.7 + foot[:, 1] * 0.3)
    cent = foot[order[np.linspace(0, len(foot) - 1, k).astype(int)]][:, :2].copy()
    lab = np.zeros(len(foot), dtype=int)
    for _ in range(25):
        lab = ((foot[:, None, :2] - cent[None]) ** 2).sum(-1).argmin(1)
        for j in range(k):
            if (lab == j).any():
                cent[j] = foot[lab == j][:, :2].mean(0)

    gaps = []
    for j in range(k):
        g = foot[lab == j]
        if not len(g):
            continue
        q = g[g[:, 2].argmin()]
        gz = terrain_z_at(prepared, float(q[0]), float(q[1]), float(q[2]))
        if gz is not None:
            gaps.append(float(q[2]) - gz)
    if len(gaps) < 2:
        return None, gaps, len(gaps)
    return max(gaps) - min(gaps), sorted(gaps), len(gaps)


def multi_support_embed_delta(gaps, embed_depth=0.10):
    """Rigid Z translation that puts every measured support below terrain."""
    valid = [float(g) for g in gaps if g is not None]
    if len(valid) < 2:
        return None
    return -max(0.0, max(valid) + max(0.0, float(embed_depth)))


def embed_multi_support_upright(obj, measure, prepared, k=4, embed_depth=0.10,
                                recompute=True):
    """Lower a rigid upright asset until all sampled supports are embedded.

    The object is not tilted, deformed, or rescaled.  On a steep slope the
    downhill support may be buried deeply; that is the deliberate compromise for
    wide structures that must remain vertical at their Composer location.
    """
    _spread, before, n = contact_spread(measure, prepared, k=k, recompute=recompute)
    dz = multi_support_embed_delta(before, embed_depth=embed_depth)
    if dz is None:
        return None
    obj.location.z += dz
    import bpy as _b
    _b.context.view_layer.update()
    spread_after, after, n_after = contact_spread(
        measure, prepared, k=k, recompute=recompute)
    return {
        "supports": n, "gaps_before": before, "applied_dz": dz,
        "embed_depth": float(embed_depth), "supports_after": n_after,
        "gaps_after": after, "spread_after": spread_after,
    }


def _fit_plane(pts):
    """SVD 擬合平面 -> (法線, 是否退化)。退化 = 取樣點近共線，撐不出一個面。"""
    import numpy as np
    centred = pts - pts.mean(0)
    u, s, vt = np.linalg.svd(centred)
    normal = vt[-1]
    if normal[2] < 0:
        normal = -normal
    # s[1] 是「平面內第二個方向」的伸展量。它相對 s[0] 太小 = 點排成一條線，
    # 此時最小奇異值方向在垂直於該線的整個平面裡都差不多，解不唯一(實測會吐出
    # 90° 的水平法線，把車整台翻倒)。
    degenerate = bool(s[0] < 1e-9 or s[1] < 0.10 * s[0])
    return normal, degenerate


def support_normal(terrain, loc, radius, n_ring=8, up_offset=60.0, verbose=False):
    """在 footprint 範圍內取樣地形，用最小平方擬合一個支撐平面 -> (法線, 地面高度)。

    ★為什麼不是單點法線★：摩托車是跨越一段地形的長條物體，單點法線代表不了它
    實際接觸的那一片。取樣一圈再擬合平面，就是「bbox 內 regression 平均法向量」。

    `terrain` 可以是單一物件，也可以是 terrain_candidates() 回傳的清單 —— 給清單時
    每條射線依序試，第一個打中的算數(LOD 分塊補不到的地方由全域地形接手)。

    ⚠️ 離群點用「對擬合平面的殘差」篩，★不能用中位數高度篩★：斜坡上上坡/下坡兩端
       本來就離中位數很遠，用高度篩會剛好砍掉帶有坡度資訊的點，剩下的近似一條等高線
       ⇒ 擬合退化。這正是 0803 那個 90° outlier 的成因。
    """
    import numpy as np
    import math
    _, mathutils = _bpy()
    Vector = mathutils.Vector

    prepared = prepare_terrains(terrain)
    if not prepared:
        return Vector((0, 0, 1)), None

    offsets = [(0.0, 0.0)]
    if radius > 1e-6:
        for k in range(n_ring):
            a = 2 * math.pi * k / n_ring
            offsets.append((radius * math.cos(a), radius * math.sin(a)))

    hits = []
    for dx, dy in offsets:
        origin = Vector((loc.x + dx, loc.y + dy, loc.z + up_offset))
        for ev, mw, inv, rot3 in prepared:
            try:
                layers = _down_hits(ev, inv, origin)
            except RuntimeError:
                continue                 # 這個地形不可 ray_cast ⇒ 換下一個候選
            if layers:
                # 多層(洞頂 + 洞底)時取離 Composer 高度 loc.z 最近的那層
                lh, ln = min(layers, key=lambda h: abs(float((mw @ h[0]).z) - loc.z))
                hits.append((mw @ lh, (rot3 @ ln).normalized()))
                break                    # 打中就算數，不再試下一個候選地形
    if not hits:
        # ★出聲★：以前這裡靜默回傳垂直法線，讓「打空」長得跟「地真的是平的」一樣，
        #   0803 的 conform 就是這樣整晚看起來像沒生效。
        if verbose:
            print(f"[anchor] ⚠️ ray_cast 全部打空 @ {tuple(round(v, 1) for v in loc)} "
                  f"⇒ 沿用垂直，不做貼合", flush=True)
        return Vector((0, 0, 1)), None
    if len(hits) < 3:
        return hits[0][1], hits[0][0].z

    pts = np.array([[p.x, p.y, p.z] for p, _ in hits])
    normal, degenerate = _fit_plane(pts)
    if not degenerate:
        # 用對平面的殘差再篩一次離群點(突出的石塊、坑洞)，然後重擬合
        resid = np.abs((pts - pts.mean(0)) @ normal)
        thresh = max(2.0 * float(np.median(resid)), 0.10)
        keep = pts[resid <= thresh]
        if len(keep) >= 3:
            normal, degenerate = _fit_plane(keep)

    if degenerate:
        # 撐不出平面就退回「射線法線的平均」—— 比一個解不唯一的 SVD 方向可信
        avg = Vector((0, 0, 0))
        for _, n in hits:
            avg += n
        normal = np.array(avg.normalized()[:]) if avg.length > 1e-6 else np.array([0.0, 0.0, 1.0])
        if verbose:
            print(f"[anchor] 取樣退化(近共線) @ {tuple(round(v, 1) for v in loc)} "
                  f"⇒ 改用射線法線平均", flush=True)

    nrm = Vector(normal).normalized()
    # 坡度上限：超過就往垂直收，避免極端擬合把物體翻倒
    tilt = math.degrees(math.acos(max(-1.0, min(1.0, nrm.z))))
    if tilt > MAX_TILT_DEG:
        nrm = Vector((0, 0, 1)).slerp(nrm, MAX_TILT_DEG / tilt)
        if verbose:
            print(f"[anchor] 坡度 {tilt:.0f}° 超過上限 ⇒ 收到 {MAX_TILT_DEG:.0f}°", flush=True)
    return nrm, float(np.median(pts[:, 2]))


def anchor_rotation(normal, normal_fac: float, yaw: float):
    """把物體的 +Z 朝 `normal` 轉，強度 normal_fac，再繞自身 Z 轉 yaw。

    等價於 infinigen 的 AlignEulerToVector(axis=Z, Factor=normal_fac) 後接
    RotateEuler(AXIS_ANGLE, Z, yaw)，只是改用四元數在 Python 端算。
    """
    _, mathutils = _bpy()
    Vector, Euler = mathutils.Vector, mathutils.Euler
    up = Vector((0, 0, 1))
    if normal_fac <= 0 or normal is None:
        return Euler((0, 0, yaw))
    target = up.slerp(Vector(normal).normalized(), min(1.0, normal_fac))
    align = up.rotation_difference(target)
    return (align @ Euler((0, 0, yaw)).to_quaternion()).to_euler()


def place(obj, terrain, loc, anchoring: str | None, yaw: float,
          footprint_radius: float | None = None, verbose: bool = False,
          support=None, bbox_objs=None, altitude_override: float | None = None):
    """把 obj 依 anchoring 擺到 loc。回傳實際用到的 (normal_fac, altitude)。

    `bbox_objs` 指定★沉降時要量哪些物件的最低點★，預設 None = 量 obj 整棵階層。

      ⚠️ ★populate 後補沉降一定要給這個★(2026-08-04 實測)：那時 obj 是
        placeholder box，資產是它的 child，而 world_bbox(obj) 量的是
        ★box ∪ 資產★的聯集。樹的 box 是 4m 高、原點在正中心(local z -2~+2)，
        底面比摩托車低 ⇒ 聯集最低點由 box 決定 ⇒ 「盒底貼地、原點停在 +2m」，
        掛在原點的摩托車就浮在離地 2.6m 的空中(20 台實測中位數 +2.61m)。
        傳資產物件進來才會量到輪胎。

    `support` 給 (法線, 地面高度) 就★跳過 ray_cast★直接用。呼叫端如果要擺很多個
    物體，應該先在★場景還沒被改動時★一次算完所有支撐面(見 support_normal 的說明)，
    否則每擺一個就會讓 depsgraph 變髒、大地形的 BVH 被重建一次。

    步驟(對應老師講的那條流水線)：
      ① 估計支撐面法線(footprint 內 regression)
      ② 沿法線旋轉(normal_fac 插值) + 繞 Z 隨機轉
      ③ 沉降：★底面接地點對真實地形 ray_cast★ + altitude 偏移
         —— ①的擬合平面只用來決定傾角，★高度不用它★(見第③步的說明)
    airborne/submerged 跳過 ①③，只做高度偏移 —— 它們根本不接觸地面。
    afloat 仍走完整的液面支撐與沉降，只是不隨表面法線傾斜，並略微吃水。
    """
    _, mathutils = _bpy()
    Vector = mathutils.Vector
    normal_fac, altitude = params_for(anchoring)
    if altitude_override is not None:
        altitude = float(altitude_override)
    mode = anchoring or DEFAULT_ANCHORING

    if mode in AIRBORNE_MODES:
        obj.rotation_euler = anchor_rotation(None, 0.0, yaw)
        obj.location = Vector((loc.x, loc.y, loc.z + altitude))
        return normal_fac, altitude

    if mode in NO_RESETTLE_MODES:
        # ★位置原封不動★，只套 yaw —— 見 NO_RESETTLE_MODES 的說明。
        obj.rotation_euler = anchor_rotation(None, 0.0, yaw)
        obj.location = Vector((loc.x, loc.y, loc.z))
        if verbose:
            print(f"[anchor] {mode}: 維持原位置，不重新落地", flush=True)
        return normal_fac, 0.0

    measure = bbox_objs if bbox_objs else obj

    # ① footprint 半徑：沒給就從 bbox 估(長條物體用長邊的一半)
    if support is not None:
        normal, ground_z = support
    else:
        if footprint_radius is None:
            lo, hi = world_bbox(measure)
            footprint_radius = float(max(hi[0] - lo[0], hi[1] - lo[1]) / 2) if lo is not None else 0.0
        normal, ground_z = support_normal(terrain, loc, footprint_radius, verbose=verbose)

    # ② 旋轉
    obj.rotation_euler = anchor_rotation(normal, normal_fac, yaw)
    obj.location = Vector((loc.x, loc.y, ground_z if ground_z is not None else loc.z))

    # ③ 沉降：轉完之後才量，否則傾斜會讓它重新穿地
    import bpy as _b
    _b.context.view_layer.update()

    # ★高度基準是真實地形，不是擬合平面★ —— 用資產底面的接地點逐點 ray_cast。
    #   擬合平面只負責②的傾角(它代表一片地的平均走向，那正是我們要的)；拿它當高度
    #   基準會在低模硬折面上差到 0.5m，8/20 台車因此陷進地裡(2026-08-04)。
    prepared = prepare_terrains(terrain) if terrain is not None else []
    gap, n_hit, n_pts = contact_gap(measure, prepared)
    if n_hit:
        obj.location.z += -gap + altitude
        if verbose:
            print(f"[anchor] {mode}: 接地點 {n_hit}/{n_pts} 命中, "
                  f"對真實地形修正 {-gap:+.2f}m", flush=True)
        return normal_fac, altitude

    # 退路：地形打不到(或沒有幾何)就沿用舊的「bbox 最低點貼擬合平面」
    if verbose:
        print(f"[anchor] ⚠️ {mode}: 接地點 ray_cast 全部打空 ⇒ 退回擬合平面沉降",
              flush=True)
    lo, _hi = world_bbox(measure)
    if lo is not None:
        target_z = (ground_z if ground_z is not None else loc.z) + altitude
        obj.location.z += target_z - float(lo[2])
    return normal_fac, altitude


def read_anchoring(factory_txt) -> str | None:
    """從 factory.txt 讀 `anchoring:`；沒有這行就回 None(呼叫端維持原本行為)。"""
    from pathlib import Path
    p = Path(factory_txt)
    if not p.is_file():
        return None
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("anchoring:"):
            v = line.split(":", 1)[1].strip()
            return v if v in ANCHORINGS else None
    return None
