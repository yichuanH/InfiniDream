"""
placeholder_colors —— 給 placeholder box 依「物件種類」上色，讓 coarse render 可讀。

★動機★：白盒 render 看不出哪些盒子是什麼。before/after 對照時，
  「樹的盒子消失了、灌木的盒子露出來」看起來會像「所有盒子都變了」(2026-07-28 實測踩到)。
  上色之後 ★同色 = 同類★，一眼就能看出哪一類被動過。

★之後的用途(不只是這次對照)★
  - hero / grime / ground_scatter 三類用不同色 → 直觀呈現 0727 第三節的分類
  - ADDED 用特殊色 → 之後 director 的 edit_plan 可以直接渲成「編輯預覽圖」
  - 給 paper 當 pipeline 示意圖

⚠️ ★材質必須用 diffuse 不能用 emission★(2026-07-28 實測)：
   emission 每個面亮度一樣 → 盒子變成「一塊色塊」，★看不出是立體的 box★。
   diffuse 才有頂面/側面明暗差，稜角才看得出來。詳見 _make_shaded_mat。

★安全性★：實測 coarse blend 的 placeholder ★完全沒有 material★(mats=[])，
  所以指定 material 不會覆蓋掉任何東西。而且 placeholder 本來就 hide_render=True，
  ★不入鏡★，上色不影響正式 render。

★配色原則★(照 0727 的 placement 三分類 + 語意分群)
  同一「類」用同一色系，不同 factory seed ★共用同色★(它們是同一種東西)。
"""
import hashlib
import re
import bpy
from candidate_colors import CANDIDATE_PALETTE, candidate_color

# 與 placement.py L288 同一條 regex
PH_COLL_RE = re.compile(r"placeholders:((.*)\((\d*)\))")

# ---------------------------------------------------------------- 配色表
# (r, g, b) 0-1。刻意選高彩度、彼此拉開的顏色，白盒背景是土黃地形。
PALETTE = {
    # ── 樹木類(綠) ──
    "TreeFactory":            (0.15, 0.75, 0.25),
    "TreeFlowerFactory":      (0.35, 0.85, 0.45),
    "BranchFactory":          (0.10, 0.50, 0.20),
    "PalmTreeFactory":        (0.30, 0.80, 0.30),
    "BushFactory":            (0.85, 0.75, 0.10),   # 灌木(黃)——與樹拉開
    "CactusFactory":          (0.60, 0.80, 0.20),

    # ── 石頭/地質(灰藍) ──
    "BoulderFactory":         (0.35, 0.45, 0.65),
    "BlenderRockFactory":     (0.25, 0.35, 0.55),
    "GlowingRocksFactory":    (0.45, 0.65, 0.90),

    # ── 生物(紅/橙) ──
    "DragonflyFactory":       (0.95, 0.35, 0.15),
    "BeetleFactory":          (0.85, 0.25, 0.10),
    "CrustaceanFactory":      (0.90, 0.45, 0.20),
    "FishFactory":            (0.95, 0.55, 0.25),
    # ⚠️ 不要用純紅 —— ★紅色保留給語意色 ADDED★，混用會分不出
    #    「這是新加的」還是「這是肉食動物」(2026-07-28 撒老虎時踩到)
    "CarnivoreFactory":       (0.85, 0.35, 0.55),   # 洋紅
    "HerbivoreFactory":       (0.90, 0.50, 0.35),
    "BirdFactory":            (0.95, 0.65, 0.30),
    "FlyingBirdFactory":      (0.98, 0.75, 0.40),

    # ── 水生植物(青) ──
    "KelpFactory":            (0.15, 0.70, 0.65),
    "SeaweedFactory":         (0.20, 0.80, 0.75),
    "CoralFactory":           (0.90, 0.40, 0.60),
    "MolluskFactory":         (0.75, 0.55, 0.85),

    # ── 其他 ──
    "CloudFactory":           (0.90, 0.90, 0.95),
    "MushroomFactory":        (0.80, 0.35, 0.75),
}

# ★語意色★：不是 factory 種類，而是「編輯狀態」。給 before/after 對照圖用。
SEMANTIC = {
    "KEPT":    (0.55, 0.55, 0.58),   # 灰——沒動過的
    # Step-8 focused review keeps old placeholders only as spatial context.
    # Neutral gray prevents them from being mistaken for requested assets.
    "CONTEXT": (0.30, 0.32, 0.35),
    "DELETED": (0.95, 0.15, 0.15),   # 紅——被刪掉的(渲染 before 時標出來)
    # ★ADDED 用紅色★：要在一堆綠(樹)黃(灌木)裡★一眼認出「這是這輪加的」★，
    #   紅色是唯一不會跟既有 factory 配色撞的高彩度色。
    "ADDED":   (0.95, 0.10, 0.10),
    # ★CULLED★：populate 的 view cull 會跳過的(不在相機軌跡視野內) →
    #   ★這些 box 在 fine 裡不會長出任何東西★。染成暗灰讓它「視覺上退場」，
    #   對比之下亮色的就是「真的會生出來的那批」。
    "CULLED":  (0.16, 0.16, 0.18),
}

FALLBACK = (0.70, 0.70, 0.72)

# Requested additions get a deterministic per-edit color in Step-8 previews.
# Identity still comes from the text label; color is a redundant visual cue.
REQUESTED_PALETTE = (
    (0.00, 0.82, 1.00),  # cyan
    (1.00, 0.24, 0.72),  # magenta
    (1.00, 0.52, 0.04),  # orange
    (0.30, 0.90, 0.32),  # green
    (0.64, 0.44, 1.00),  # violet
    (1.00, 0.28, 0.22),  # red
)

_RUNTIME_COLORS = {}

# These semantic identities appear in the placement-loop demo and must retain
# the exact same color even when one review round contains only a subset.  The
# fallback below is also subset-independent, so adding/removing another asset
# never renumbers the colors of assets already present.
REQUESTED_ID_COLORS = {
    "eagle_glide": REQUESTED_PALETTE[0],
    "giraffe_forward": REQUESTED_PALETTE[1],
    "motorcycle_wander": REQUESTED_PALETTE[2],
}


def requested_colors(labels):
    """Return a stable ``edit_id -> RGB`` map for one preview."""
    unique = sorted({str(label) for label in labels if label})
    result = {}
    for label in unique:
        if label in REQUESTED_ID_COLORS:
            result[label] = REQUESTED_ID_COLORS[label]
            continue
        digest = hashlib.sha256(label.encode("utf-8")).digest()
        result[label] = REQUESTED_PALETTE[int.from_bytes(digest[:8], "big")
                                           % len(REQUESTED_PALETTE)]
    return result


def _make_emission_mat(name, rgb, strength=1.0):
    """做一個★自發光★material。

    ★★不要用 emission★★(2026-07-28 實測踩到)：
      emission 不受光照影響 = ★每個面亮度完全一樣★ → 盒子看起來是「一塊色塊」，
      ★完全沒有立體感，看不出是 box★。
    ⇒ 改用 ★diffuse★：頂面/側面/背光面因為法線不同而有明暗差 → ★盒子看得出稜角★。

    加一點 emission 當 ★底光(ambient floor)★，讓完全背光的面不會黑到分不出顏色，
    但 strength 壓很低(0.12)，不能蓋掉 diffuse 的明暗變化。
    """
    mat = bpy.data.materials.get(name)
    if mat is not None:
        return mat
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")

    # ★主體是 diffuse★ —— 這是立體感的來源
    dif = nt.nodes.new("ShaderNodeBsdfDiffuse")
    dif.inputs["Color"].default_value = (*rgb, 1.0)

    # ★底光★：避免背光面全黑，但要壓低才不會洗掉明暗
    emi = nt.nodes.new("ShaderNodeEmission")
    emi.inputs["Color"].default_value = (*rgb, 1.0)
    emi.inputs["Strength"].default_value = 0.12 * strength

    add = nt.nodes.new("ShaderNodeAddShader")
    nt.links.new(dif.outputs["BSDF"], add.inputs[0])
    nt.links.new(emi.outputs["Emission"], add.inputs[1])
    nt.links.new(add.outputs["Shader"], out.inputs["Surface"])

    # viewport 也給個色，方便手動開 blend 檢查
    mat.diffuse_color = (*rgb, 1.0)
    return mat


def _make_translucent_shaded_mat(name, rgb, alpha=1.0):
    """Create a shaded candidate material; alpha=1 is fully opaque."""
    alpha = max(0.05, min(0.95, float(alpha)))
    mat = bpy.data.materials.get(name)
    if mat is not None:
        return mat
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    transparent = nt.nodes.new("ShaderNodeBsdfTransparent")
    diffuse = nt.nodes.new("ShaderNodeBsdfDiffuse")
    diffuse.inputs["Color"].default_value = (*rgb, 1.0)
    mix = nt.nodes.new("ShaderNodeMixShader")
    mix.inputs[0].default_value = alpha
    nt.links.new(transparent.outputs["BSDF"], mix.inputs[1])
    nt.links.new(diffuse.outputs["BSDF"], mix.inputs[2])
    nt.links.new(mix.outputs["Shader"], out.inputs["Surface"])
    mat.diffuse_color = (*rgb, alpha)
    return mat


def set_translucent_box_material(obj, rgb, *, alpha=1.0, key="candidate"):
    """Override one preview-only box with a shaded material."""
    if obj is None or obj.type != "MESH" or not obj.data:
        return False
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", str(key))
    mat = _make_translucent_shaded_mat(
        f"PH_TRANSLUCENT_{safe}_{int(round(float(alpha) * 100))}",
        rgb,
        alpha=alpha,
    )
    obj.data.materials.clear()
    obj.data.materials.append(mat)
    return True


def color_for_factory(classname):
    return PALETTE.get(classname, FALLBACK)


def colorize_placeholders(strength=1.0, semantic_map=None, requested_map=None,
                          verbose=True):
    """把場景裡所有 placeholder box 依 factory 類名上色。

    semantic_map: 可選 {object_name: "DELETED"/"ADDED"/"KEPT"}，
                  ★有指定的物件改用語意色★，其餘用 factory 色。

    ⚠️ ★只碰 root placeholder 物件本身★，不走訪 children ——
       0726 血淚：走訪帶 GN 的物件會觸發求值 → segfault。
       placeholder root 是 8 頂點純 mesh，安全。
    """
    semantic_map = semantic_map or {}
    requested_map = requested_map or {}
    stats = {}
    for coll in bpy.data.collections:
        m = PH_COLL_RE.fullmatch(coll.name)
        if not m:
            continue
        _full, classname, _seed = m.groups()
        roots = [o for o in coll.objects if o.parent is None]
        for o in roots:
            # ⚠️ cave 場景實測有 placeholder ★沒有 mesh data★(o.data is None) ⇒ 會炸
            if o.type != "MESH" or not o.data:
                continue
            requested = requested_map.get(o.name)
            tag = semantic_map.get(o.name)
            if requested:
                label, rgb = requested
                safe = re.sub(r"[^A-Za-z0-9_]+", "_", str(label))
                mat_name = f"PH_REQUESTED_{safe}"
                key = f"REQUESTED:{label}"
                _RUNTIME_COLORS[key] = rgb
            elif tag:
                rgb = SEMANTIC[tag]
                mat_name = f"PH_{tag}"
                key = tag
            else:
                rgb = color_for_factory(classname)
                mat_name = f"PH_{classname}"
                key = classname
            mat = _make_emission_mat(mat_name, rgb, strength)
            o.data.materials.clear()
            o.data.materials.append(mat)
            stats[key] = stats.get(key, 0) + 1

    if verbose:
        print("[color] placeholder 上色:", flush=True)
        for k, n in sorted(stats.items(), key=lambda kv: -kv[1]):
            rgb = SEMANTIC.get(k) or _RUNTIME_COLORS.get(k) or color_for_factory(k)
            print(f"[color]   {k:26} n={n:5}  rgb=({rgb[0]:.2f},{rgb[1]:.2f},{rgb[2]:.2f})",
                  flush=True)
    return stats


def rescale_to_real_size(mode="skeleton", wire=False, verbose=True):
    """★把 placeholder box 縮到「實際物體大小」★，讓 coarse 預覽圖不再誤導。

    ★問題(2026-07-28 使用者指出)★：
      placeholder box 是 ★寫死的 4×4×4★(trees/generate.py:97 spawn_cube(size=4))，
      ★與實際物體大小無關★。實測：
          BushFactory  box 4×4×4  vs 實際 0.73×0.72×★1.3★   → ★box 大 3 倍★
          TreeFactory  box 4×4×4  vs 實際 4.0×4.1×★7.5★     → ★box 反而太小★
      ⇒ box ★不是 bounding box★，只是「位置標記」(標位置/方位/scale + view cull)。
      ⇒ 直接渲 box 會讓看圖的人(或 critic API)★嚴重誤判物體大小與密度★。

    ★做法★：讀 ★child 骨架的 dimensions★ 當實際尺寸，改 box 的顯示大小。
    ⚠️ ★只改 mesh 頂點座標，不動 object.scale★ ——
       object.scale 會被 populate 繼承(0728 6-C: asset 是 parent 到 placeholder 底下)，
       ★動它會真的改變長出來的東西★，也會改變 view cull 結果(實測 20→17)。
       改 mesh data 只影響「這顆 box 畫多大」，★對 populate 完全無副作用★。

    mode: "skeleton" 用 child 骨架的實際 dimensions
    wire: True → 同時把 box 改成★線框★(只留邊，不遮擋後面的東西)
    """
    import bmesh
    from mathutils import Vector

    stats, no_skel = {}, 0
    for coll in bpy.data.collections:
        m = PH_COLL_RE.fullmatch(coll.name)
        if not m:
            continue
        _full, classname, _seed = m.groups()
        for o in [x for x in coll.objects if x.parent is None]:
            if o.type != "MESH" or not o.data:
                continue
            if not o.children:
                no_skel += 1
                continue          # creature 類沒有骨架子物件 → 保持原樣
            target = o.children[0].dimensions
            cur = o.dimensions
            if min(cur) <= 1e-6:
                continue
            # 每軸各自的縮放比(box 是正方體，實際物體通常不是)
            fx, fy, fz = (target.x / cur.x, target.y / cur.y, target.z / cur.z)
            # ★改 mesh 頂點，不改 object.scale★
            me = o.data
            for v in me.vertices:
                v.co = Vector((v.co.x * fx, v.co.y * fy, v.co.z * fz))
            # box 原本以原點為中心 → 縮完後往上移半高，讓底部貼地
            for v in me.vertices:
                v.co.z += target.z * 0.5
            stats[classname] = stats.get(classname, 0) + 1

    if wire:
        for coll in bpy.data.collections:
            if not PH_COLL_RE.fullmatch(coll.name):
                continue
            for o in [x for x in coll.objects if x.parent is None]:
                if o.type != "MESH" or not o.data:
                    continue
                md = o.modifiers.new("wire", "WIREFRAME")
                md.thickness = 0.06
                md.use_replace = True

    if verbose:
        print(f"[size] 依實際骨架尺寸縮放 placeholder box:", flush=True)
        for k, n in sorted(stats.items(), key=lambda kv: -kv[1]):
            print(f"[size]   {k:26} n={n}", flush=True)
        if no_skel:
            print(f"[size]   ⚠️ {no_skel} 個沒有骨架子物件(creature 類)，維持原大小",
                  flush=True)
    return stats


def legend_lines(stats):
    """給 README / log 用的圖例文字。"""
    out = []
    for k, n in sorted(stats.items(), key=lambda kv: -kv[1]):
        rgb = SEMANTIC.get(k) or _RUNTIME_COLORS.get(k) or color_for_factory(k)
        hexs = "#%02X%02X%02X" % tuple(int(c * 255) for c in rgb)
        out.append(f"{hexs}  {k}  (n={n})")
    return out


def marker_boxes(wire=False, thickness=0.08, verbose=True):
    """★把每個 placeholder 一律換成「bbox 尺寸的方塊」★(2026-08-09)

    ★為什麼需要★：「placeholder 長什麼樣」是★每個 factory 自己決定的★ ——
      ・TreeFactory  → spawn_cube(size=4)，真的是方塊(planner/out/candy_forest 那批)
      ・BoulderFactory → ★50 頂點的低模石頭★，不是方塊
      ⇒ 雪山場景(只有 167 個 boulder + 2 隻鳥)渲出來是「地上一堆淺藍小石頭」，
        看不出「標示位置與大小」的意思。使用者要的是統一的標記框，
        那就★不能靠 factory 原本的幾何★。

    做法：讀該物件目前的 bbox(有骨架子物件就用骨架的實際尺寸)，把 mesh 重建成
      一個貼地的方塊(8 頂點)。wire=True 再加 WIREFRAME modifier 變成★線框★，
      不遮擋後面的東西。

    ⚠️ ★只改 mesh data，絕不動 object.scale★ —— scale 會被 populate 繼承
       (0728 6-C)，動它會真的改變長出來的東西，也會改變 view cull 結果。
       這裡改的只是「這顆標記畫多大」。
    ⚠️ 這是★預覽用★的破壞性修改，只在渲染那份 in-memory blend 上做，不要存檔。
    """
    import bmesh
    from mathutils import Vector

    n, n_skel = 0, 0
    for coll in bpy.data.collections:
        if not PH_COLL_RE.fullmatch(coll.name):
            continue
        for o in [x for x in coll.objects if x.parent is None]:
            if o.type != "MESH" or not o.data:
                continue
            # 實際尺寸：優先用骨架子物件(樹/灌木)，否則用自己的 bbox
            if o.children:
                dim = o.children[0].dimensions.copy()
                n_skel += 1
            else:
                dim = o.dimensions.copy()
            sx, sy, sz = (max(dim.x, 0.2), max(dim.y, 0.2), max(dim.z, 0.2))
            me = o.data
            bm = bmesh.new()
            bmesh.ops.create_cube(bm, size=1.0)
            for v in bm.verts:                       # 縮到實際尺寸 + 底面貼地
                v.co.x *= sx
                v.co.y *= sy
                v.co.z = v.co.z * sz + sz * 0.5
            bm.to_mesh(me)
            bm.free()
            if wire:
                for md in list(o.modifiers):
                    o.modifiers.remove(md)
                md = o.modifiers.new("wire", "WIREFRAME")
                md.thickness = thickness
                md.use_replace = True
            n += 1
    if verbose:
        print(f"[marker] {n} 個 placeholder 換成 bbox 方塊"
              f"({n_skel} 個用骨架尺寸、{n - n_skel} 個用自身 bbox)"
              f"{'，線框' if wire else ''}", flush=True)
    return n
