"""
camera_frustum —— 把「populate 實際會看到的範圍」畫成可見的線框，疊在 coarse render 上。

★動機(2026-07-28)★：俯視 coarse render 會看到「有 placeholder box，但 fine 裡沒長東西」
  的區域，看起來像 bug。其實是 populate 的 ★view cull★：
    placement.py:151-188 filter_populate_targets()
      compute_inview_distances(..., dist_max=dist_cull, vis_margin=vis_cull)
      if not mask.any(): continue      ← ★不在任何相機視野內 → 直接跳過★
    generate_nature.py:861  populate_all(TreeFactory, primary_cams, vis_cull=4)
      (dist_cull 用預設 200)
  ⇒ 231 個 placeholder 只有 20 個被 populate = ★8.7%★。

  ★把視錐畫出來，這件事就從「要知道的暗規則」變成「看得見的東西」★。

★重要★：判準是 ★整段相機軌跡(frame 1..72)★，不是單一幀 ——
  populate 收到的是 primary_cams 而 compute_inview_distances 會掃所有幀。
  所以這裡預設也掃整段軌跡，畫出 ★掃掠體(swept volume)★ 的邊界。

畫的東西：
  1. ★視錐線框★：每個取樣幀畫一個四角錐(相機原點 → 遠平面四角)
  2. ★遠平面外框★：把各幀的遠平面連起來，形成掃掠範圍
  3. 顏色：亮青色 emission，在土黃地形上很顯眼

⚠️ 用 ★curve/line 物件★(不是 mesh 面)，避免遮住底下的 placeholder box。
"""
import bpy
import math
from mathutils import Vector


def _frustum_corners(cam, scene, dist):
    """相機在目前 frame 的視錐四角(世界座標，距離 dist 處)。"""
    # view_frame 回傳相機空間的四角(在 z=-1 平面上)
    frame = cam.data.view_frame(scene=scene)
    mw = cam.matrix_world
    origin = mw.translation.copy()
    corners = []
    for v in frame:
        # v 在相機 local space，z=-1。等比例縮放到 dist
        p_local = v * dist
        corners.append(mw @ p_local)
    return origin, corners


def _make_line_mat(name, rgb, strength=6.0):
    mat = bpy.data.materials.get(name)
    if mat:
        return mat
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    emi = nt.nodes.new("ShaderNodeEmission")
    emi.inputs["Color"].default_value = (*rgb, 1.0)
    emi.inputs["Strength"].default_value = strength
    nt.links.new(emi.outputs["Emission"], out.inputs["Surface"])
    mat.diffuse_color = (*rgb, 1.0)
    return mat


def _polyline(name, points, radius, mat, cyclic=False):
    """用 curve 畫一條有粗細的線(bevel_depth 讓它在 render 裡看得見)。"""
    cu = bpy.data.curves.new(name, "CURVE")
    cu.dimensions = "3D"
    sp = cu.splines.new("POLY")
    sp.points.add(len(points) - 1)
    for i, p in enumerate(points):
        sp.points[i].co = (p.x, p.y, p.z, 1.0)
    sp.use_cyclic_u = cyclic
    cu.bevel_depth = radius
    cu.bevel_resolution = 1
    ob = bpy.data.objects.new(name, cu)
    ob.data.materials.append(mat)
    bpy.context.scene.collection.objects.link(ob)
    return ob


def draw_inview_markers(scene, placeholders_by_coll, cams=None, dist_cull=200.0,
                        vis_cull=4.0, radius=1.2, verbose=True):
    """★真正有用的那個★：不畫視錐線，而是★把「populate 看得到的 placeholder」標出來★。

    ★為什麼比畫視錐好★(2026-07-28 實測)：
      視錐是 3D 錐體，投影到俯視圖上只是幾條交叉的線，★看不出「哪一塊地被涵蓋」★。
      而使用者真正想知道的是「哪些 box 會生、哪些不會」——
      ★直接標在 box 上最直接★。

    做法：★直接呼叫 populate 用的那支 filter_populate_targets()★，
    拿到的就是「populate 會生成的那批」，不是我自己重推的近似。
    ⇒ ★這保證圖上標的與實際行為一致★(不是二手轉述 —— 0727 2-C 的教訓)。

    回傳 {collection_name: set(in_view object names)}
    """
    from infinigen.core.placement import placement as _pl

    if cams is None:
        cams = [o for o in bpy.data.objects if o.type == "CAMERA"]
    # populate 收到的是 primary_cams(整個 rig 的相機清單)
    result = {}
    for coll_name, roots in placeholders_by_coll.items():
        try:
            targets = _pl.filter_populate_targets(
                list(roots), cams, dist_cull, vis_cull, verbose=False)
        except Exception as e:
            print(f"[inview] {coll_name} 失敗: {e}", flush=True)
            continue
        names = {p.name for p, _d, _v in targets}
        result[coll_name] = names
        if verbose:
            print(f"[inview] {coll_name[:50]:50} in_view={len(names):4} / {len(roots):4}",
                  flush=True)
    return result


def draw_frustum(scene, cams=None, dist=None, n_samples=5, radius=0.35,
                 rgb=(0.1, 1.0, 1.0), verbose=True):
    """在場景裡畫出相機軌跡的視錐線框。

    cams:       要畫的相機(None = 用 scene 裡所有 camera)
    dist:       遠平面距離。★預設 200 = populate 的 dist_cull 預設值★
    n_samples:  沿 frame_start..frame_end 取幾個幀來畫
    """
    if dist is None:
        dist = 200.0            # placement.populate_all 的 dist_cull 預設
    if cams is None:
        cams = [o for o in bpy.data.objects if o.type == "CAMERA"]
    if not cams:
        raise SystemExit("[frustum] 場景沒有相機")

    mat_edge = _make_line_mat("FRUSTUM_EDGE", rgb)
    mat_far = _make_line_mat("FRUSTUM_FAR", (1.0, 0.55, 0.05))

    f0, f1 = scene.frame_start, scene.frame_end
    if n_samples <= 1:
        frames = [f0]
    else:
        frames = [int(round(f0 + (f1 - f0) * i / (n_samples - 1)))
                  for i in range(n_samples)]

    made = 0
    far_rings = []
    orig_frame = scene.frame_current
    # ★只畫主相機★(立體聲對的兩台幾乎重合，畫兩台只會糊成一團)
    cam = scene.camera if scene.camera in cams else cams[0]

    for fr in frames:
        scene.frame_set(fr)
        bpy.context.view_layer.update()
        origin, corners = _frustum_corners(cam, scene, dist)
        # 四條稜線：相機原點 → 遠平面四角
        for k, c in enumerate(corners):
            _polyline(f"frustum_edge_f{fr}_{k}", [origin, c], radius, mat_edge)
            made += 1
        # 遠平面外框
        _polyline(f"frustum_far_f{fr}", list(corners), radius * 1.4, mat_far, cyclic=True)
        made += 1
        far_rings.append(corners)

    # ★把各幀的遠平面同一角連起來★ = 掃掠範圍的側邊
    if len(far_rings) > 1:
        for k in range(4):
            pts = [ring[k] for ring in far_rings]
            _polyline(f"frustum_sweep_{k}", pts, radius, mat_far)
            made += 1

    scene.frame_set(orig_frame)
    bpy.context.view_layer.update()
    if verbose:
        print(f"[frustum] 相機={cam.name} dist={dist} frames={frames} "
              f"→ 畫了 {made} 條線", flush=True)
    return made
