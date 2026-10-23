"""
render_view —— 用★同一個相機視角★渲染 coarse(白盒) 或 populate 後(真樹) 的 blend。

★這支的存在理由★：before/after 對照必須★同視角同設定★，否則看起來的差異
  可能來自相機而不是編輯。所以 coarse 版和 fine 版共用這一支，只差
  --show-placeholders 旗標。

視角預設用 ★aerial(俯視)★ —— 使用者要求「俯視一點的視角，看很多白色 box」。
  aerial 參數比 director/render_coarse.py 的 raised(pitch=16) 更高更俯，
  才看得到成片的 box 分佈；pitch 越大越往下看。

★沿用 director/render_coarse.py 的兩個血淚坑★:
  坑1(0726) ★不要走訪 coll.all_objects 去設 hide_render★ —— 2206 個帶 GN 的物件，
        逐物件走訪會觸發 GN 求值 → ★segfault★。只翻 collection 層級旗標。
  坑2      placeholders collection 預設 hide_render=True(base_nature.gin 的
        group_collections.config)，★不手動打開會渲出一片空地★。

★--color★: 依 factory 類別給 placeholder box 上色(見 placeholder_colors.py)。
  白盒版看不出哪些盒子是什麼種類 —— before/after 對照時「樹盒消失、灌木盒露出」
  看起來會像「所有盒子都變了」。上色後 ★同色=同類★，一眼看出哪類被動過。

★--frustum★: 畫出★原始相機的視錐線框★(見 camera_frustum.py)。
  俯視圖會看到「有 box 但 fine 沒長東西」的區域 —— 那是 populate 的 view cull
  (231 個 placeholder 只長 20 個 = 8.7%)。把視錐畫出來，
  ★這件事就從「要知道的暗規則」變成「看得見的東西」★。
  可用 --frustum-dist 改遠平面距離(預設 200 = populate 的 dist_cull 預設值)。

用法:
  python render_view.py -- <blend> <out_png> [--show-placeholders] [--color]
                           [--frustum] [--frustum-dist D] [--frustum-frames N]
                           [--frame N]
                           [--skip-armature-repair]
                           [--mark-requested-manifest manifest.json]
                           [--pitch P] [--aerial A] [--back B] [--side S] [--yaw Y]
                           [--res RESX RESY] [--samples N] [--view XFORM] [--exp E]
"""
import bpy, sys, math, os, re
from pathlib import Path
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
import placeholder_colors as pc
import camera_frustum as cf

argv = sys.argv[sys.argv.index("--") + 1:]
BLEND = argv[0]
OUT_PNG = argv[1]
rest = argv[2:]
REUSE_OPEN = "--reuse-open" in rest
SKIP_ARMATURE_REPAIR = "--skip-armature-repair" in rest

SHOW_PH = "--show-placeholders" in rest
# ★--hide-placeholders★(2026-09-09)：coarse 場景裡的 placeholder ★是可見的★
#   (等 populate 換成真資產)。用 --as-is 直接出圖時，一個 TreeFactory 的包圍盒
#   就吃掉了畫面下半部 51% 的可見表面 —— scatter 回歸測試的 before/after 有一半
#   被那些方塊擋住，根本判讀不了「這支 scatter 改變了什麼」。
#   ⚠️ 它★只影響出圖★，散佈早就做完了；草其實有撒在盒子底下的地形上，
#      populate 之後盒子變成細樹幹，那些草本來就會露出來。
HIDE_PH = "--hide-placeholders" in rest
DO_COLOR = "--color" in rest
DO_FRUSTUM = "--frustum" in rest
MARK_INVIEW = "--mark-inview" in rest
LABEL_ADDED = "--label-added" in rest
LABEL_SCREEN_POSITION = "--label-screen-position" in rest
INSTANCE_COLORS = "--instance-colors" in rest
TOP_VIEW_REQUESTED = "--top-view-requested" in rest
CANDIDATE_PINS = "--candidate-pins" in rest
CANDIDATE_BOX_LABEL = (
    rest[rest.index("--candidate-box") + 1]
    if "--candidate-box" in rest else None)
SEMANTIC_GRID_OVERLAY = "--semantic-grid-overlay" in rest
SEMANTIC_GRID_DATA = (
    Path(rest[rest.index("--semantic-grid-data") + 1])
    if "--semantic-grid-data" in rest else None)
_semantic_grid_payload = None
if SEMANTIC_GRID_DATA is not None and SEMANTIC_GRID_DATA.is_file():
    import json as _json
    _semantic_grid_payload = _json.loads(
        SEMANTIC_GRID_DATA.read_text(encoding="utf-8"))
# ★--mark-added <report.json>★: 讀 apply_edit_plan 產的 report，把新增的 placeholder
#   標成 ★青色★，一眼看出「這一輪加了什麼、加在哪」。給 critic(API-2) 看的圖用。
MARK_ADDED = rest[rest.index("--mark-added") + 1] if "--mark-added" in rest else None
_added_names = set()
if MARK_ADDED:
    import json as _json
    _rep = _json.loads(Path(MARK_ADDED).read_text(encoding="utf-8"))
    _added_names = set(_rep.get("added_placeholder_names", []))
    print(f"[render] 從 report 讀到 {len(_added_names)} 個新增的 placeholder", flush=True)
MARK_REQUESTED_MANIFEST = (
    Path(rest[rest.index("--mark-requested-manifest") + 1])
    if "--mark-requested-manifest" in rest else None)
_requested_manifest = None
if MARK_REQUESTED_MANIFEST is not None:
    if not MARK_REQUESTED_MANIFEST.is_file():
        raise SystemExit(
            f"[render] --mark-requested-manifest missing: {MARK_REQUESTED_MANIFEST}")
    import json as _json
    _requested_manifest = _json.loads(
        MARK_REQUESTED_MANIFEST.read_text(encoding="utf-8"))
    print(
        f"[render] 從 manifest 讀 requested placement: {MARK_REQUESTED_MANIFEST}",
        flush=True)

def _get(flag, default, n=1, cast=float):
    if flag not in rest:
        return default if n == 1 else default
    i = rest.index(flag)
    vals = [cast(rest[i + 1 + k]) for k in range(n)]
    return vals[0] if n == 1 else vals

# ★俯視配置★(比 render_coarse.py 的 raised 更高更俯，才看得到成片 box)
AERIAL  = _get("--aerial", 28.0)
PITCH   = _get("--pitch", 42.0)
BACK    = _get("--back", 18.0)
SIDE    = _get("--side", 0.0)
YAW     = _get("--yaw", 0.0)
RES     = _get("--res", [1280, 720], n=2, cast=int)
RES_X, RES_Y = (RES if isinstance(RES, list) else [1280, 720])
SAMPLES = int(_get("--samples", 64))
VIEW_X  = rest[rest.index("--view") + 1] if "--view" in rest else "Standard"
EXPOSURE = _get("--exp", -1.5)

print(f"[render] blend={BLEND}", flush=True)
print(f"[render] show_placeholders={SHOW_PH} aerial={AERIAL} pitch={PITCH} "
      f"back={BACK} side={SIDE} yaw={YAW} samples={SAMPLES}", flush=True)

if not REUSE_OPEN:
    bpy.ops.wm.open_mainfile(filepath=BLEND)
# 見 animation_visibility.py：rigged 資產的 armature 留在 hide_render 的母本
# collection ⇒ 不修的話身體永遠 rest pose。只動 armature 可見性，不產生像素。
if SKIP_ARMATURE_REPAIR:
    print("[render] --skip-armature-repair：保留 blend 內既有 rig 狀態", flush=True)
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from animation_visibility import repair_armature_visibility  # noqa: E402
    repair_armature_visibility()
scene = bpy.context.scene
cam = scene.camera or next((o for o in scene.objects if o.type == "CAMERA"), None)
if cam is None:
    raise SystemExit("[render] 沒有相機")
scene.camera = cam
# Preview Stage-C altitude without persisting it. apply_plan leaves these
# placeholders at their support/base point so placer applies the offset once to
# real geometry. This includes the small negative waterline offset for afloat.
if "--preview-anchoring" in rest and not bpy.context.scene.get(
        "bi_preview_anchoring_applied", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import placement_modes as _pm
    for _obj in scene.objects:
        _anchoring = _obj.get("bi_anchoring")
        if (_obj.parent is None
                and (_anchoring in _pm.AIRBORNE_MODES or _anchoring == "afloat")):
            _normal_fac, _altitude = _pm.params_for(_anchoring)
            _obj.location.z += float(_altitude)
    bpy.context.view_layer.update()
    bpy.context.scene["bi_preview_anchoring_applied"] = True

# Exact requested identity is stamped by apply_plan and the placement manifest.
# The manifest is needed for replacement/attrs edits: those boxes already exist,
# so they are not listed in added_placeholder_names but still belong to this
# Composer request.
_requested_labels = {}
_requested_color_keys = {}
_request_group_members = {}
_request_group_color_keys = {}
_request_group_labels = {}
_request_group_modes = {}
_annotation_names = set()


def _root_placeholders(_collection):
    return sorted(
        (o for o in _collection.objects if o.parent is None),
        key=lambda o: (
            int(o.get("bi_instance_index"))
            if o.get("bi_instance_index") is not None else 1_000_000,
            o.name,
        ),
    )


def _register_requested(_obj, *, label, color_key, group_key, annotate=False):
    if _obj is None:
        return
    _name = str(_obj.name)
    _group_key = str(group_key or label or _name)
    _color_key = str(color_key or _group_key)
    _requested_labels[_name] = str(label or _group_key)
    _requested_color_keys[_name] = _color_key
    _request_group_members.setdefault(_group_key, set()).add(_name)
    _request_group_color_keys.setdefault(_group_key, _color_key)
    _request_group_labels.setdefault(_group_key, _group_key)
    if annotate:
        _annotation_names.add(_name)


_manifest_driven_colors = _requested_manifest is not None
for _name in sorted(_added_names):
    _obj = bpy.data.objects.get(_name)
    if _obj is None:
        continue
    _base_label = str(_obj.get("bi_code_from") or _name)
    _instance_index = _obj.get("bi_instance_index")
    _label = str(
        _obj.get("bi_preview_label")
        or (_base_label + (f" #{int(_instance_index)}"
                           if _instance_index is not None else "")))
    _color_key = (
        _base_label if _manifest_driven_colors
        else _label if INSTANCE_COLORS
        else str(_obj.get("bi_preview_color_key") or _base_label)
    )
    _register_requested(
        _obj, label=_label, color_key=_color_key, group_key=_base_label,
        annotate=True)
    _request_group_modes.setdefault(_base_label, "add")

if _requested_manifest is not None:
    for _placement in _requested_manifest.get("placements", []) or []:
        _code_from = str(_placement.get("code_from") or "")
        _collection_name = str(_placement.get("collection") or "")
        if not _code_from or not _collection_name:
            continue
        _collection = bpy.data.collections.get(_collection_name)
        if _collection is None:
            continue
        _roots = _root_placeholders(_collection)
        _added_count = int(_placement.get("added_this_plan") or 0)
        _request_group_modes[_code_from] = (
            "add" if _added_count > 0 else "replace_existing_placeholder_slots")
        for _index, _obj in enumerate(_roots):
            _instance_index = _obj.get("bi_instance_index")
            _base_label = str(_obj.get("bi_code_from") or _code_from)
            if _added_count > 0:
                _label = str(
                    _obj.get("bi_preview_label")
                    or (_base_label + (f" #{int(_instance_index)}"
                                       if _instance_index is not None
                                       else f" #{_index}")))
                _annotate = _obj.name in _added_names or not _added_names
            else:
                _label = _code_from
                _annotate = False
            _register_requested(
                _obj, label=_label, color_key=_code_from,
                group_key=_code_from, annotate=_annotate)

if _manifest_driven_colors:
    _unique_color_keys = sorted(set(_requested_color_keys.values()))
    _requested_colors_by_label = {
        _key: pc.REQUESTED_PALETTE[_index % len(pc.REQUESTED_PALETTE)]
        for _index, _key in enumerate(_unique_color_keys)
    }
else:
    _requested_colors_by_label = pc.requested_colors(
        _requested_color_keys.values())
_requested_map = {
    name: (label, _requested_colors_by_label[_requested_color_keys[name]])
    for name, label in _requested_labels.items()
}
_request_legend_items = []
for _group_key in sorted(_request_group_members):
    _color_key = _request_group_color_keys.get(_group_key, _group_key)
    _rgb = _requested_colors_by_label.get(_color_key)
    if _rgb is None:
        continue
    _request_legend_items.append({
        "label": _request_group_labels.get(_group_key, _group_key),
        "count": len(_request_group_members[_group_key]),
        "mode": _request_group_modes.get(_group_key, "request"),
        "rgb": _rgb,
    })
_all_candidate_names = {
    name for name, label in _requested_labels.items()
    if re.fullmatch(r"B\d+-C\d+|S\d+-C\d+", label)
}
_candidate_pin_names = _all_candidate_names if CANDIDATE_PINS else set()
_annotation_requested_map = {
    name: _requested_map[name]
    for name in sorted(_annotation_names)
    if name in _requested_map
}
_candidate_box_name = None
if CANDIDATE_BOX_LABEL is not None:
    _matches = [
        name for name in _all_candidate_names
        if _requested_labels.get(name) == CANDIDATE_BOX_LABEL
    ]
    if len(_matches) != 1:
        raise SystemExit(
            f"[render] --candidate-box {CANDIDATE_BOX_LABEL!r} matched "
            f"{len(_matches)} objects")
    _candidate_box_name = _matches[0]
rig = cam
while rig.parent is not None:
    rig = rig.parent
base_loc = rig.location.copy()
base_rot = cam.rotation_euler.copy()
# Contract renders must use a frame that fine_terrain actually covered.  Keep
# the historical frame-1 behavior for diagnostic/off-axis modes.
RENDER_FRAME = int(_get("--frame", scene.frame_start)) if "--as-is" in rest else 1
scene.frame_set(RENDER_FRAME)
print(f"[render] frame={RENDER_FRAME}", flush=True)

# ── ★畫視錐★ ──
# ⚠️ ★必須在「套俯視視角」之前做★ —— 視錐要畫的是 ★原始相機★ 的涵蓋範圍
#    (也就是 populate 當初據以 cull 的那台)。等一下相機會被搬到俯視位置，
#    那之後再畫就會畫成俯視鏡頭的視錐，完全是另一回事。
if DO_FRUSTUM:
    cf.draw_frustum(
        scene,
        dist=_get("--frustum-dist", 200.0),
        n_samples=int(_get("--frustum-frames", 5)),
    )

# ── ★算「populate 看得到哪些 placeholder」★ ──
# ⚠️ 同樣★必須在搬相機之前★ —— 判準是原始相機軌跡。
_inview_names = None
if MARK_INVIEW:
    _by_coll = {}
    for coll in bpy.data.collections:
        if pc.PH_COLL_RE.fullmatch(coll.name):
            _by_coll[coll.name] = [o for o in coll.objects if o.parent is None]
    _cams = [o for o in bpy.data.objects if o.type == "CAMERA"]
    _res = cf.draw_inview_markers(scene, _by_coll, cams=_cams,
                                  dist_cull=_get("--frustum-dist", 200.0))
    _inview_names = set()
    for s in _res.values():
        _inview_names |= s
    print(f"[render] populate 看得到的 placeholder 共 {len(_inview_names)} 個", flush=True)

# ── 套視角(結構與 composer.py L82-103 / render_coarse.py L41-61 一致) ──
# ★--as-is★: ★完全不動相機★，用 blend 裡本來那台(2026-08-09)
#   photographer 把選定的 pose ★bake 進 blend★ 之後，這支就不該再套任何偏移 ——
#   再套一次就又變成「兩台相機」，那正是今天要修掉的 bug。
#   ⚠️ 注意「全部參數給 0」★不等於★ as-is：下面會把 pitch 設成絕對值
#      (90-PITCH)，給 0 等於強迫水平，會蓋掉 bake 好的俯角。
AS_IS = "--as-is" in rest
if AS_IS:
    print("[render] ★--as-is：不動相機，用 blend 裡 bake 好的那台★", flush=True)

for c in ([] if AS_IS else list(cam.constraints)):
    cam.constraints.remove(c)
for c in ([] if AS_IS else list(rig.constraints)):
    rig.constraints.remove(c)
if not AS_IS:
    cam.animation_data_clear()
    rig.animation_data_clear()
    rig.location = base_loc.copy()
    rig.location.z += AERIAL
    cam.rotation_mode = "XYZ"
    cam.rotation_euler = (math.radians(90 - PITCH), 0, base_rot.z + math.radians(YAW))
    rig.rotation_mode = "XYZ"
    rig.rotation_euler = (0, 0, rig.rotation_euler.z)
bpy.context.view_layer.update()
if not AS_IS:
    deps = bpy.context.evaluated_depsgraph_get()
    m = cam.evaluated_get(deps).matrix_world
    fwd = -Vector((m[0][2], m[1][2], m[2][2])).normalized()
    right = Vector((m[0][0], m[1][0], m[2][0])).normalized()
    rig.location -= fwd * BACK
    rig.location += right * SIDE
    bpy.context.view_layer.update()

# ── ★--eval-view★ scene-level evaluation viewpoints (2026-09-15) ────────────
# 為什麼要有這個：populate 是★照 hero 相機★ cull 的(placement.py filter_populate_targets
#   + official_scatter 的視錐測試)，畫面外的 placeholder 根本沒長東西。所以多視角評估
#   ★不能★把相機搬去任意位置 —— 那會拍到成片空地。
#   這支只做「繞著同一份已 populate 的內容小幅度換角度」：相機到內容的★距離不變★
#   ⇒ spawn_asset(distance=) 烤進去的 LOD 仍然成立，各 view 的資產精度一致。
#   用法： --eval-view az-25 / az+25 / az0 / top
#   ⚠️ 必須放在 --frustum / --mark-inview 之後 —— 那兩者算的是原始相機。
EVAL_VIEW = (rest[rest.index("--eval-view") + 1]
             if "--eval-view" in rest else None)
if EVAL_VIEW:
    import numpy as _np
    _deps = bpy.context.evaluated_depsgraph_get()
    _m = cam.evaluated_get(_deps).matrix_world.copy()
    _cam_pos = _m.translation.copy()
    _fwd = -Vector((_m[0][2], _m[1][2], _m[2][2])).normalized()
    _fwd_h = Vector((_fwd.x, _fwd.y, 0.0))
    if _fwd_h.length < 1e-6:
        _fwd_h = Vector((1.0, 0.0, 0.0))
    _fwd_h.normalize()
    # populate 出來的真資產都在 unique_assets:* 裡(placer/populate_collection 的 target)
    _pts = []
    for _c in bpy.data.collections:
        if not _c.name.startswith("unique_assets:"):
            continue
        for _o in _c.objects:
            if _o.parent is None and _o.type in {"MESH", "CURVE", "EMPTY"}:
                _pts.append(_o.matrix_world.translation.copy())
    _front = [(pt, (pt - _cam_pos).dot(_fwd)) for pt in _pts]
    _front = [(pt, d) for pt, d in _front if 2.0 < d < 150.0]
    if _front:
        _arr = _np.array([[pt.x, pt.y, pt.z] for pt, _ in _front], dtype=float)
        _target = Vector(_np.median(_arr, axis=0).tolist())
        _d_med = float(_np.median([d for _, d in _front]))
    else:
        # 沒有 populate 資產(純地形場景)時退回「相機前方 25m 的地面高度」
        print("[render] ⚠️ --eval-view 找不到 unique_assets 內容，退回 25m 前方點",
              flush=True)
        _d_med = 25.0
        _target = _cam_pos + _fwd * _d_med
    for _c in list(cam.constraints):
        cam.constraints.remove(_c)
    for _c in list(rig.constraints):
        rig.constraints.remove(_c)
    cam.animation_data_clear()
    rig.animation_data_clear()
    _spec = str(EVAL_VIEW).strip().lower()
    if _spec == "top":
        # 俯視：放在★內容中心★正上方(不是相機正上方 —— 相機腳下那塊本來就空，
        #   而且在 hero 視錐邊緣，OcMesher 不保證有切地形)。
        #   高度取到「畫面短邊剛好蓋住半徑 d_med 的圓」。
        _fov_y = float(cam.data.angle_y)
        _cover = max(_d_med, 15.0)
        _h = min(_cover / max(math.tan(_fov_y * 0.5), 1e-3), 200.0)
        _new_pos = Vector((_target.x, _target.y, _target.z + _h))
        # 影像上方 = hero 相機的前進方向(local +Y 轉到世界 (-sin,cos,0))
        _yaw = math.atan2(-_fwd_h.x, _fwd_h.y)
        _new_rot = (0.0, 0.0, _yaw)
        print(f"[render] eval-view top: h={_h:.1f}m cover_r={_cover:.1f}m "
              f"target=({_target.x:.1f},{_target.y:.1f},{_target.z:.1f})", flush=True)
    else:
        _mm = re.fullmatch(r"az([+-]?\d+(?:\.\d+)?)", _spec)
        if _mm is None:
            raise SystemExit(f"[render] --eval-view 只接受 az<deg> 或 top（收到 {EVAL_VIEW!r}）")
        _dz = math.radians(float(_mm.group(1)))
        # 繞★內容中心的鉛直軸★轉，半徑與高度都不變 ⇒ 到內容的距離不變 ⇒ LOD 有效
        _off = _cam_pos - _target
        _cs, _sn = math.cos(_dz), math.sin(_dz)
        _new_pos = _target + Vector((_off.x * _cs - _off.y * _sn,
                                     _off.x * _sn + _off.y * _cs,
                                     _off.z))
        _dirv = (_target - _new_pos)
        _new_rot = tuple(_dirv.to_track_quat("-Z", "Y").to_euler())
        print(f"[render] eval-view az{float(_mm.group(1)):+.0f}: "
              f"r={_off.length:.1f}m d_med={_d_med:.1f}m n_assets={len(_front)} "
              f"target=({_target.x:.1f},{_target.y:.1f},{_target.z:.1f})", flush=True)
    if cam.parent is not None:
        butil_parent = cam.parent
        cam.matrix_parent_inverse = butil_parent.matrix_world.inverted()
    cam.rotation_mode = "XYZ"
    cam.location = _new_pos if cam.parent is None else (
        cam.parent.matrix_world.inverted() @ _new_pos)
    cam.rotation_euler = _new_rot
    if cam.parent is not None:
        cam.parent.rotation_euler = (0.0, 0.0, 0.0)
        cam.parent.location = (0.0, 0.0, 0.0)
        cam.matrix_parent_inverse.identity()
        cam.location = _new_pos
    cam.data.clip_end = max(float(cam.data.clip_end), 600.0)
    bpy.context.view_layer.update()

# Diagnostic top view: use a temporary unparented orthographic camera centered
# on the requested instances.  This is never saved back to the blend and never
# replaces the final camera for visibility/composition contracts.
if TOP_VIEW_REQUESTED:
    if not _requested_map and _semantic_grid_payload is None:
        raise SystemExit(
            "[render] --top-view-requested needs requested placeholders "
            "or --semantic-grid-data")
    points = []
    for _name in _requested_map:
        _obj = bpy.data.objects.get(_name)
        if _obj is None:
            continue
        points.extend(_obj.matrix_world @ Vector(corner) for corner in _obj.bound_box)
    if _semantic_grid_payload is not None:
        for _cell in _semantic_grid_payload.get("cells", []):
            _world = _cell.get("world_position") or [0.0, 0.0, 0.0]
            _z = float(_world[2]) if len(_world) >= 3 else 0.0
            for _xy in _cell.get("world_xy_polygon") or []:
                if len(_xy) >= 2:
                    points.append(Vector((float(_xy[0]), float(_xy[1]), _z)))
    if not points:
        raise SystemExit("[render] top view found no requested bounds")
    min_x, max_x = min(p.x for p in points), max(p.x for p in points)
    min_y, max_y = min(p.y for p in points), max(p.y for p in points)
    max_z = max(p.z for p in points)
    span_x = max(max_x - min_x, 1.0)
    span_y = max(max_y - min_y, 1.0)
    # Include enough surrounding terrain to read ice/water/shore support while
    # keeping the requested boxes legible. ortho_scale is the vertical span.
    aspect = float(RES_X) / max(float(RES_Y), 1.0)
    ortho_scale = max(span_y * 2.4, span_x * 2.4 / aspect, 18.0)
    top_data = bpy.data.cameras.new("BI_PlacementTopDiagnostic")
    top_data.type = "ORTHO"
    top_data.ortho_scale = ortho_scale
    top_data.clip_start = 0.1
    top_data.clip_end = max(500.0, max_z + ortho_scale * 4.0)
    top_cam = bpy.data.objects.new("BI_PlacementTopDiagnostic", top_data)
    scene.collection.objects.link(top_cam)
    top_cam.location = Vector(((min_x + max_x) * 0.5,
                               (min_y + max_y) * 0.5,
                               max_z + max(50.0, ortho_scale * 2.0)))
    top_cam.rotation_euler = (0.0, 0.0, 0.0)
    scene.camera = top_cam
    print(f"[render] top diagnostic center=({top_cam.location.x:.2f}, "
          f"{top_cam.location.y:.2f}) scale={ortho_scale:.2f}", flush=True)
    if _semantic_grid_payload is not None:
        _hidden_ceiling = 0

        def _looks_like_ceiling(_name):
            _lower = str(_name or "").casefold()
            return "ceiling" in _lower or "room_ceil" in _lower

        for _coll in bpy.data.collections:
            if _looks_like_ceiling(_coll.name):
                if not _coll.hide_render:
                    _hidden_ceiling += 1
                _coll.hide_render = True
        for _obj in bpy.data.objects:
            if _obj.type != "MESH":
                continue
            if not (
                    _looks_like_ceiling(_obj.name)
                    or any(_looks_like_ceiling(_coll.name)
                           for _coll in _obj.users_collection)):
                continue
            if not _obj.hide_render:
                _hidden_ceiling += 1
            _obj.hide_render = True
        if _hidden_ceiling:
            print(
                f"[render] top semantic grid hid {_hidden_ceiling} ceiling objects/collections",
                flush=True)
    bpy.context.view_layer.update()

# ── 關掉 placeholder(scatter 回歸測試用) ──
#   跟 SHOW_PH 一樣★只翻 collection 層級★，不要走訪 all_objects(會 segfault)。
#   但 coarse 場景有些 placeholder 是掛在 collection 外的獨立物件，那些要按名字
#   個別關 —— 實測擋住畫面的 TreeFactory(3040046).spawn_placeholder(69) 就是
#   物件層級 hide_render=False，只關 collection 關不掉它。
if HIDE_PH:
    _n_coll = 0
    for coll in bpy.data.collections:
        if coll.name.startswith("placeholders"):
            coll.hide_render = True
            _n_coll += 1

    def _hide_ph(lc):
        if lc.collection.name.startswith("placeholders"):
            lc.exclude = True
        for ch in lc.children:
            _hide_ph(ch)
    _hide_ph(bpy.context.view_layer.layer_collection)

    _n_obj = 0
    for _obj in bpy.data.objects:
        if "spawn_placeholder" in _obj.name and not _obj.hide_render:
            _obj.hide_render = True
            _n_obj += 1
    bpy.context.view_layer.update()
    print(f"[render] ★--hide-placeholders：關掉 {_n_coll} 個 collection、"
          f"{_n_obj} 個獨立 placeholder 物件★", flush=True)

# ── 打開 placeholder(只有 coarse 版需要) ──
# ⚠️ ★坑1★: 只翻 collection 層級，★不要走訪 all_objects★(會 segfault)
if SHOW_PH:
    n = 0
    for coll in bpy.data.collections:
        if coll.name.startswith("placeholders"):
            coll.hide_render = False
            n += 1

    def _unhide(lc):
        if lc.collection.name.startswith("placeholders"):
            lc.exclude = False
            lc.hide_viewport = False
        for ch in lc.children:
            _unhide(ch)
    _unhide(bpy.context.view_layer.layer_collection)
    bpy.context.view_layer.update()
    print(f"[render] 打開 {n} 個 placeholder collection", flush=True)

    # ── ★上色★ ──
    # ★--real-size★: 把 box 縮到實際物體大小(預設的 4×4×4 會誤導)
    # ★--wire★: 改成線框，不遮擋後面的東西
    if "--real-size" in rest or "--wire" in rest:
        pc.rescale_to_real_size(wire=("--wire" in rest))
    # ★--marker-box★: ★一律換成 bbox 尺寸的方塊★(2026-08-09)
    #   rescale 只縮既有幾何，救不了「factory 的 placeholder 本來就不是方塊」的場景
    #   —— BoulderFactory 生的是 50 頂點低模石頭，雪山場景渲出來是一堆小石頭，
    #   看不出「標示位置與大小」。這個旗標無視原本形狀，統一畫成貼地方塊。
    #   搭配 --wire 就是線框標記框。
    if "--marker-box" in rest:
        pc.marker_boxes(
            wire=("--wire" in rest),
            thickness=float(_get("--marker-thickness", 0.08)),
        )

    if DO_COLOR:
        semantic = {}
        if LABEL_ADDED:
            # Existing scene placeholders are context, not review targets. Use
            # one neutral color so yellow bushes/cacti cannot be mistaken for
            # multiple copies of a requested asset.
            for coll in bpy.data.collections:
                if not pc.PH_COLL_RE.fullmatch(coll.name):
                    continue
                for _obj in coll.objects:
                    if _obj.parent is None and _obj.name not in _requested_map:
                        semantic[_obj.name] = "CONTEXT"
        # ★ADDED 優先於 CULLED★ —— 新增的東西一定要看得到，
        #   就算它落在視野外(那本身就是要讓 critic 發現的問題)
        for _n in _added_names:
            if _n not in _requested_map:
                semantic[_n] = "ADDED"
        if _inview_names is not None:
            # 不在 populate 視野內 → 標成 CULLED(暗灰)，視覺上退場
            for coll in bpy.data.collections:
                if not pc.PH_COLL_RE.fullmatch(coll.name):
                    continue
                for o in coll.objects:
                    if (o.parent is None and o.name not in _inview_names
                            and o.name not in semantic):   # ★不覆蓋 ADDED★
                        semantic[o.name] = "CULLED"
            print(f"[render] 標暗 {len(semantic)} 個 populate 看不到的 placeholder",
                  flush=True)
        stats = pc.colorize_placeholders(
            semantic_map=semantic,
            requested_map=_requested_map if LABEL_ADDED else None,
        )
        print("[render] 圖例:", flush=True)
        for line in pc.legend_lines(stats):
            print(f"[render]   {line}", flush=True)

# Candidate selection uses full-size boxes in the saved temporary blend for
# deterministic clipping/overlap audits.  Hide only those alternatives from the
# Cycles image and draw exact screen-space pins after rendering; ten perspective
# boxes otherwise obscure both each other and the support surface.  FIXED boxes
# remain full size in the image.
if CANDIDATE_PINS:
    for _name in _candidate_pin_names:
        _candidate_obj = bpy.data.objects.get(_name)
        if _candidate_obj is not None:
            _candidate_obj.hide_render = True
elif _candidate_box_name is not None:
    # Contact-sheet mode: each render contains exactly one full-size candidate
    # box.  Other alternatives are hidden, while already-correct instances stay
    # visible as neutral FIXED context.  Contact-sheet boxes are opaque: this
    # prevents background water/reflections seen through a proxy from being
    # mistaken for the actual support below its bottom edge.
    for _name in _all_candidate_names:
        _candidate_obj = bpy.data.objects.get(_name)
        if _candidate_obj is not None:
            _candidate_obj.hide_render = _name != _candidate_box_name
    _candidate_rgb = pc.candidate_color(CANDIDATE_BOX_LABEL)
    pc.set_translucent_box_material(
        bpy.data.objects.get(_candidate_box_name),
        _candidate_rgb,
        alpha=float(_get("--candidate-box-alpha", 1.0)),
        key=CANDIDATE_BOX_LABEL,
    )
    _annotation_requested_map = {}
    for _name, (_label, _rgb) in _requested_map.items():
        if _name == _candidate_box_name:
            _annotation_requested_map[_name] = (_label, _candidate_rgb)
        elif "FIXED" in _label.upper():
            _fixed_rgb = (0.48, 0.50, 0.54)
            pc.set_translucent_box_material(
                bpy.data.objects.get(_name), _fixed_rgb,
                alpha=float(_get("--fixed-box-alpha", 1.0)), key="fixed")
            _annotation_requested_map[_name] = (_label, _fixed_rgb)

# ── render ──
scene.render.engine = "CYCLES"
scene.cycles.samples = SAMPLES
scene.cycles.use_denoising = True
scene.view_settings.view_transform = VIEW_X
scene.view_settings.exposure = EXPOSURE
# ★裝置設定統一走 render_device★ —— 舊寫法在找不到 GPU 時會靜默退回 CPU
#   (實測同場景 17 秒 → 82 分鐘)，現在一定會印出醒目警告。
import render_device
render_device.configure(scene, label="render")
scene.render.resolution_x = RES_X
scene.render.resolution_y = RES_Y
scene.render.image_settings.file_format = "PNG"
scene.render.filepath = OUT_PNG
bpy.ops.render.render(write_still=True)

if SEMANTIC_GRID_OVERLAY:
    if _semantic_grid_payload is None:
        raise SystemExit(
            "[render] --semantic-grid-overlay needs --semantic-grid-data")
    from PIL import Image, ImageDraw, ImageFont
    _grid = _semantic_grid_payload
    _image = Image.open(OUT_PNG).convert("RGBA")
    _draw = ImageDraw.Draw(_image)
    _cells = _grid.get("cells", [])
    _w, _h = _image.size
    _legend = _grid.get("surface_inventory") or []

    def _load_font(size):
        try:
            return ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                int(size))
        except OSError:
            return ImageFont.load_default()

    _cell_font_size = max(9, min(16, _w // 80 + 4))
    _legend_font_size = max(10, min(14, _w // 120 + 6))
    _cell_font = _load_font(_cell_font_size)
    _legend_font = _load_font(_legend_font_size)
    _palette = [
        (45, 190, 255), (255, 177, 56), (74, 226, 128),
        (255, 103, 132), (184, 143, 255), (64, 220, 202),
        (255, 224, 88), (121, 167, 255), (255, 137, 69),
        (148, 234, 88), (244, 120, 255), (96, 198, 149),
    ]

    def _plane_key(_cell):
        return str(
            _cell.get("support_plane_id")
            or _cell.get("region_id")
            or "/".join(map(str, _cell.get("surface_types") or []))
            or "surface")

    _plane_keys = sorted({_plane_key(_cell) for _cell in _cells})
    _plane_colors = {
        _key: _palette[_index % len(_palette)]
        for _index, _key in enumerate(_plane_keys)
    }

    def _adjust(_rgb, _factor):
        return tuple(
            max(0, min(255, int(round(_channel * _factor))))
            for _channel in _rgb)

    def _cell_colors(_cell):
        _base = _plane_colors.get(_plane_key(_cell), (46, 166, 96))
        try:
            _parity = (int(_cell.get("column") or 0)
                       + int(_cell.get("row") or 0)) % 2
        except (TypeError, ValueError):
            _parity = 0
        _fill_rgb = _adjust(_base, 1.12 if _parity else 0.82)
        _outline_rgb = _adjust(_base, 1.30)
        return (*_fill_rgb, 88), (*_outline_rgb, 210)

    def _short_mesh(_value):
        _text = str(_value or "")
        _text = re.sub(
            r"\(\d+\)\.(?:spawn_asset|spawn_placeholder|bbox_placeholder)\(\d+\)",
            "", _text)
        return _text[:36]

    _plane_legend = {}
    for _entry in _legend:
        _key = str(_entry.get("plane_id") or _entry.get("support_plane_id") or "")
        if not _key:
            continue
        _surface = "/".join(map(str, _entry.get("surface_types") or []))
        _mesh = _short_mesh(_entry.get("surface_object"))
        _cells_n = _entry.get("usable_cell_count")
        _plane_legend[_key] = " ".join(part for part in (
            _key,
            _surface or None,
            _mesh or None,
            f"n={_cells_n}" if _cells_n is not None else None,
        ) if part)

    def _screen_px(nx, ny):
        return (
            int(round(max(0.0, min(1.0, float(nx))) * (_w - 1))),
            int(round((1.0 - max(0.0, min(1.0, float(ny)))) * (_h - 1))),
        )

    def _world_px(x, y, z):
        from bpy_extras.object_utils import world_to_camera_view
        ndc = world_to_camera_view(scene, scene.camera, Vector((x, y, z)))
        return _screen_px(ndc.x, ndc.y)

    _patches = _grid.get("patches") or []
    _selected_patch_ids = {
        str(row.get("support_patch_id") or row.get("patch_id") or row.get("support_plane_id") or "")
        for row in _grid.get("selected_patches") or []
        if (row.get("support_patch_id") or row.get("patch_id")
            or row.get("support_plane_id"))
    }
    if _patches:
        _patch_font_size = max(10, min(18, _w // 64 + 5))
        _patch_font = _load_font(_patch_font_size)
        _patch_keys = []
        _patch_colors = {}
        for _index, _patch in enumerate(sorted(
                _patches,
                key=lambda item: str(
                    item.get("patch_id") or item.get("support_plane_id") or ""))):
            _key = str(_patch.get("patch_id") or _patch.get("support_plane_id") or "")
            if not _key:
                continue
            _patch_keys.append(_key)
            _patch_colors[_key] = _palette[_index % len(_palette)]

        def _patch_points(_patch):
            _polygon = []
            _source = (
                _patch.get("world_xy_polygon") if TOP_VIEW_REQUESTED
                else _patch.get("screen_polygon"))
            if not isinstance(_source, list):
                return []
            if TOP_VIEW_REQUESTED:
                _z = 0.0
                _centroid = _patch.get("world_xy_centroid")
                if isinstance(_centroid, list) and len(_centroid) >= 2:
                    _z = 0.0
                for _xy in _source:
                    if not isinstance(_xy, (list, tuple)) or len(_xy) < 2:
                        continue
                    _polygon.append(_world_px(float(_xy[0]), float(_xy[1]), _z))
            else:
                for _xy in _source:
                    if not isinstance(_xy, (list, tuple)) or len(_xy) < 2:
                        continue
                    _polygon.append(_screen_px(float(_xy[0]), float(_xy[1])))
            return _polygon

        _selected_patch_annotations = []
        for _patch in _patches:
            _key = str(_patch.get("patch_id") or _patch.get("support_plane_id") or "")
            _points = _patch_points(_patch)
            if len(_points) < 3:
                continue
            _base = _patch_colors.get(_key, (46, 166, 96))
            _fill = (*_adjust(_base, 0.95), 78)
            _outline = (*_adjust(_base, 1.15), 220)
            _draw.polygon(_points, fill=_fill)
            _draw.line(_points + [_points[0]], fill=_outline, width=4)
            _center_x = sum(point[0] for point in _points) / len(_points)
            _center_y = sum(point[1] for point in _points) / len(_points)
            _label = " ".join(part for part in (
                str(_key),
                str(_patch.get("support_kind") or ""),
                str(_patch.get("patch_label") or "").replace("_", " "),
            ) if part)[:84]
            _bbox = _draw.textbbox((0, 0), _label, font=_patch_font, stroke_width=1)
            _tw, _th = _bbox[2] - _bbox[0], _bbox[3] - _bbox[1]
            _left = int(max(4, min(_w - _tw - 18, _center_x - _tw / 2 - 10)))
            _top = int(max(4, min(_h - _th - 14, _center_y - _th / 2 - 8)))
            _draw.rounded_rectangle(
                (_left, _top, _left + _tw + 16, _top + _th + 10),
                radius=5, fill=(6, 8, 12, 196),
                outline=_outline, width=2)
            _draw.text((_left + 8, _top + 5), _label, font=_patch_font,
                       fill=(255, 255, 255, 255), stroke_width=1,
                       stroke_fill=(0, 0, 0, 255))
            if _key in _selected_patch_ids:
                _selected_patch_annotations.append((_points, _center_x, _center_y, _patch, _label))
        for _points, _x0, _y0, _patch, _label in _selected_patch_annotations:
            _draw.line(_points + [_points[0]], fill=(0, 0, 0, 255), width=10)
            _draw.line(_points + [_points[0]], fill=(255, 245, 72, 255), width=6)
            _selected_text = " ".join(part for part in (
                "SELECTED",
                _label,
                str(_patch.get("placement_modes") or []),
            ) if part)[:96]
            _bbox = _draw.textbbox((0, 0), _selected_text, font=_legend_font,
                                   stroke_width=1)
            _tw, _th = _bbox[2] - _bbox[0], _bbox[3] - _bbox[1]
            _pad_x, _pad_y = 8, 5
            _left = int(max(4, min(_w - _tw - 2 * _pad_x - 4,
                                   _x0 - _tw / 2 - _pad_x)))
            _top = int(max(4, min(_h - _th - 2 * _pad_y - 4,
                                  _y0 + 14)))
            _draw.rounded_rectangle(
                (_left, _top, _left + _tw + 2 * _pad_x,
                 _top + _th + 2 * _pad_y),
                radius=5, fill=(255, 245, 72, 238),
                outline=(0, 0, 0, 255), width=2)
            _draw.text((_left + _pad_x, _top + _pad_y), _selected_text,
                       font=_legend_font, fill=(18, 17, 10, 255),
                       stroke_width=1, stroke_fill=(255, 255, 210, 170))
        if _legend or _patch_keys:
            _legend_lines = []
            for _key in _patch_keys[:12]:
                _legend_lines.append((
                    _patch_colors.get(_key, (180, 180, 180)),
                    (str(next(
                        (_patch.get("patch_label") for _patch in _patches
                         if str(_patch.get("patch_id") or _patch.get("support_plane_id") or "") == _key),
                        _key)) or _key)[:72],
                ))
            if len(_patch_keys) > len(_legend_lines):
                _legend_lines.append((
                    (180, 185, 192),
                    f"+{len(_patch_keys) - len(_legend_lines)} patches",
                ))
            if _selected_patch_annotations:
                _legend_lines.append((
                    (255, 245, 72),
                    f"yellow outline = selected patch ({len(_selected_patch_annotations)})",
                ))
            if _legend_lines:
                _line_height = _legend_font_size + 5
                _widths = [
                    _draw.textbbox((0, 0), line, font=_legend_font,
                                   stroke_width=1)[2] + 34
                    for _rgb, line in _legend_lines
                ]
                _box_w = min(_w - 24, max(_widths, default=0) + 18)
                _box_h = len(_legend_lines) * _line_height + 12
                _draw.rounded_rectangle(
                    (12, 12, 12 + _box_w, 12 + _box_h),
                    radius=5, fill=(4, 6, 10, 155),
                    outline=(230, 235, 243, 120), width=1)
                _legend_y = 18
                for _rgb, _text in _legend_lines:
                    _draw.rectangle(
                        (20, _legend_y + 3, 36, _legend_y + 16),
                        fill=(*_rgb, 235),
                        outline=(255, 255, 255, 190))
                    _draw.text((44, _legend_y), _text, font=_legend_font,
                               fill=(230, 235, 243, 255), stroke_width=1,
                               stroke_fill=(0, 0, 0, 255))
                    _legend_y += _line_height
    else:
        _selected_annotations = []
        for _cell in _cells:
            _bounds = list(map(float, _cell.get(
                "screen_bounds") or _grid.get("screen_bounds")
                or [0.0, 0.0, 1.0, 1.0]))
            _bx0, _by0, _bx1, _by1 = _bounds
            _cols = int(_cell.get("grid_columns") or _grid.get("grid", {}).get("columns", 1))
            _rows = int(_cell.get("grid_rows") or _grid.get("grid", {}).get("rows", 1))
            _step_x = (_bx1 - _bx0) / max(_cols, 1)
            _step_y = (_by1 - _by0) / max(_rows, 1)
            _cx, _cy = map(float, _cell.get("screen_position", [0.5, 0.5]))
            _status = str(_cell.get("status") or "")
            _label = str(
                _cell.get("semantic_label")
                or _cell.get("semantic_cell_id")
                or _cell.get("support_plane_id")
                or _cell.get("region_id")
                or _cell.get("cell_id") or "")
            _fill, _outline = _cell_colors(_cell)
            _polygon = _cell.get("world_xy_polygon") or []
            _points = []
            if TOP_VIEW_REQUESTED and len(_polygon) >= 3:
                _world = _cell.get("world_position") or [0.0, 0.0, 0.0]
                _z = float(_world[2]) if len(_world) >= 3 else 0.0
                _points = [_world_px(float(_xy[0]), float(_xy[1]), _z)
                           for _xy in _polygon if len(_xy) >= 2]
                if len(_points) < 3:
                    continue
                _draw.polygon(_points, fill=_fill)
                _draw.line(_points + [_points[0]], fill=_outline, width=2)
                _x0 = sum(point[0] for point in _points) / len(_points)
                _y0 = sum(point[1] for point in _points) / len(_points)
            else:
                _screen_polygon = _cell.get("screen_polygon") or []
                _points = [
                    _screen_px(float(_xy[0]), float(_xy[1]))
                    for _xy in _screen_polygon if len(_xy) >= 2
                ]
                if len(_points) >= 3:
                    _draw.polygon(_points, fill=_fill)
                    _draw.line(_points + [_points[0]], fill=_outline, width=2)
                    _x0 = sum(point[0] for point in _points) / len(_points)
                    _y0 = sum(point[1] for point in _points) / len(_points)
                else:
                    _left, _bottom = _screen_px(
                        _cx - _step_x / 2.0, _cy - _step_y / 2.0)
                    _right, _top = _screen_px(
                        _cx + _step_x / 2.0, _cy + _step_y / 2.0)
                    _x0, _y0 = min(_left, _right), min(_top, _bottom)
                    _x1, _y1 = max(_left, _right), max(_top, _bottom)
                    _draw.rectangle([_x0, _y0, _x1, _y1],
                                    fill=_fill, outline=_outline, width=2)
                    _points = [
                        (_x0, _y0), (_x1, _y0), (_x1, _y1), (_x0, _y1)]
            if _status == "feasible":
                _bbox = _draw.textbbox((0, 0), _label, font=_cell_font,
                                       stroke_width=1)
                _tw, _th = _bbox[2] - _bbox[0], _bbox[3] - _bbox[1]
                _draw.text((_x0 - _tw / 2, _y0 - _th / 2), _label,
                           font=_cell_font, fill=(255, 255, 255, 255),
                           stroke_width=2, stroke_fill=(0, 0, 0, 255))
            if _cell.get("selected"):
                _selected_annotations.append((_points, _x0, _y0, _cell, _label))
        for _points, _x0, _y0, _cell, _label in _selected_annotations:
            if len(_points) >= 3:
                _draw.line(_points + [_points[0]], fill=(0, 0, 0, 255), width=9)
                _draw.line(_points + [_points[0]], fill=(255, 245, 72, 255), width=5)
            _selected_text = " ".join(part for part in (
                "SELECTED",
                _label,
                str(_cell.get("selected_label") or "").replace("_", " "),
            ) if part)
            _selected_text = _selected_text[:82]
            _bbox = _draw.textbbox((0, 0), _selected_text, font=_legend_font,
                                   stroke_width=1)
            _tw, _th = _bbox[2] - _bbox[0], _bbox[3] - _bbox[1]
            _pad_x, _pad_y = 8, 5
            _left = int(max(4, min(_w - _tw - 2 * _pad_x - 4,
                                   _x0 - _tw / 2 - _pad_x)))
            _top = int(max(4, min(_h - _th - 2 * _pad_y - 4,
                                  _y0 + 14)))
            _draw.rounded_rectangle(
                (_left, _top, _left + _tw + 2 * _pad_x,
                 _top + _th + 2 * _pad_y),
                radius=5, fill=(255, 245, 72, 238),
                outline=(0, 0, 0, 255), width=2)
            _draw.text((_left + _pad_x, _top + _pad_y), _selected_text,
                       font=_legend_font, fill=(18, 17, 10, 255),
                       stroke_width=1, stroke_fill=(255, 255, 210, 170))
        if _legend or _plane_keys:
            _legend_lines = []
            for _key in _plane_keys[:12]:
                _legend_lines.append((
                    _plane_colors.get(_key, (180, 180, 180)),
                    (_plane_legend.get(_key) or _key)[:72],
                ))
            if len(_plane_keys) > len(_legend_lines):
                _legend_lines.append((
                    (180, 185, 192),
                    f"+{len(_plane_keys) - len(_legend_lines)} planes",
                ))
            if _selected_annotations:
                _legend_lines.append((
                    (255, 245, 72),
                    f"yellow outline = selected grid ({len(_selected_annotations)})",
                ))
            if _legend_lines:
                _line_height = _legend_font_size + 5
                _widths = [
                    _draw.textbbox((0, 0), line, font=_legend_font,
                                   stroke_width=1)[2] + 34
                    for _rgb, line in _legend_lines
                ]
                _box_w = min(_w - 24, max(_widths, default=0) + 18)
                _box_h = len(_legend_lines) * _line_height + 12
                _draw.rounded_rectangle(
                    (12, 12, 12 + _box_w, 12 + _box_h),
                    radius=5, fill=(4, 6, 10, 155),
                    outline=(230, 235, 243, 120), width=1)
                _legend_y = 18
                for _rgb, _text in _legend_lines:
                    _draw.rectangle(
                        (20, _legend_y + 3, 36, _legend_y + 16),
                        fill=(*_rgb, 235),
                        outline=(255, 255, 255, 190))
                    _draw.text((44, _legend_y), _text, font=_legend_font,
                               fill=(230, 235, 243, 255), stroke_width=1,
                               stroke_fill=(0, 0, 0, 255))
                    _legend_y += _line_height
    _image.convert("RGB").save(OUT_PNG)

if LABEL_ADDED and (_annotation_requested_map or _request_legend_items):
    # Raster annotations are drawn after Cycles so labels remain legible and
    # screen-aligned.  Their anchors come from deterministic camera projection.
    from PIL import Image, ImageDraw, ImageFont
    import camera_visibility as _camera_visibility

    _image = Image.open(OUT_PNG).convert("RGBA")
    _draw = ImageDraw.Draw(_image)
    _font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    try:
        _font = ImageFont.truetype(
            _font_path, 16 if len(_annotation_requested_map) > 6 else 22)
    except OSError:
        _font = ImageFont.load_default()
    _width, _height = _image.size

    _legend_rows = []
    for _item in _request_legend_items[:6]:
        _rgb = tuple(int(max(0.0, min(1.0, channel)) * 255)
                     for channel in _item["rgb"])
        _label = str(_item["label"]).replace("_", " ").upper()
        _noun = (
            "TARGET SLOTS"
            if _item.get("mode") == "replace_existing_placeholder_slots"
            else "BOXES"
        )
        _legend_rows.append((
            _rgb,
            f"{_label} {_noun} = {_item['count']}",
        ))
    if len(_request_legend_items) > len(_legend_rows):
        _legend_rows.append((
            (190, 194, 200),
            f"+{len(_request_legend_items) - len(_legend_rows)} REQUEST GROUPS",
        ))
    _legend_rows.append((
        (76, 82, 90),
        "GRAY BOXES = EXISTING UNTOUCHED PLACEHOLDERS",
    ))
    _row_bboxes = [
        _draw.textbbox((0, 0), _text[:72], font=_font, stroke_width=1)
        for _rgb, _text in _legend_rows
    ]
    _legend_w = min(
        _width - 24,
        max((_bbox[2] - _bbox[0] for _bbox in _row_bboxes), default=0) + 58)
    _row_h = max(26, max(
        (_bbox[3] - _bbox[1] for _bbox in _row_bboxes), default=14) + 10)
    _legend_h = len(_legend_rows) * _row_h + 12
    _legend_top = _height - _legend_h - 12
    _draw.rounded_rectangle(
        [12, _legend_top, 12 + _legend_w, _legend_top + _legend_h], radius=7,
        fill=(40, 43, 47, 235), outline=(230, 230, 230, 255), width=2,
    )
    _legend_y = _legend_top + 8
    for _rgb, _text in _legend_rows:
        _draw.rectangle(
            [22, _legend_y + 5, 38, _legend_y + 21],
            fill=(*_rgb, 240), outline=(255, 255, 255, 230), width=1)
        _draw.text(
            (48, _legend_y + 2), _text[:72], font=_font,
            fill=(255, 255, 255, 255),
            stroke_width=1, stroke_fill=(0, 0, 0, 255),
        )
        _legend_y += _row_h

    _occupied_labels = []
    for _name, (_label, _rgb) in sorted(
            _annotation_requested_map.items(), key=lambda item: item[1][0]):
        _obj = bpy.data.objects.get(_name)
        if _obj is None:
            continue
        # Use the exact same render-visible geometry definition as Machine
        # Review. Including hidden skeleton/proxy children here could label a
        # passing visible box CLIPPED even though that helper is never rendered.
        _projection = _camera_visibility.projected_bounds(
            scene, _obj, visible_only=True)
        _bounds = _projection.get("bounds_ndc")
        _center = _projection.get("center_ndc")
        if not _bounds or not _center:
            continue
        _is_candidate_pin = _name in _candidate_pin_names
        if (_is_candidate_pin and _obj.get("bi_screen_x") is not None
                and _obj.get("bi_screen_y") is not None):
            _box_x = int(max(
                0, min(_width - 1, float(_obj["bi_screen_x"]) * _width)))
            _box_top = int(max(
                0, min(_height - 1,
                       (1.0 - float(_obj["bi_screen_y"])) * _height)))
        else:
            _box_x = int(max(0, min(_width - 1, _center[0] * _width)))
            _box_top = int(max(0, min(_height - 1, (1.0 - _bounds[3]) * _height)))
        _text = str(_label).replace("_", " ").upper()
        if (LABEL_SCREEN_POSITION and _obj.get("bi_screen_x") is not None
                and _obj.get("bi_screen_y") is not None):
            _text += (
                f" @ ({float(_obj['bi_screen_x']):.3f},"
                f" {float(_obj['bi_screen_y']):.3f})")
        _intersects_frame = bool(
            _bounds[2] >= 0.0 and _bounds[0] <= 1.0
            and _bounds[3] >= 0.0 and _bounds[1] <= 1.0)
        # ``--wire`` is a diagnostic-only modifier with 6 cm thickness. It can
        # expand a reviewed proxy by a few pixels after Machine Review has
        # already proved the underlying box is in frame. Tolerate only that
        # tiny display expansion here; the actual geometry contract remains
        # strict in camera_visibility.audit_anchor().
        _wire_label_tolerance = 0.005 if "--wire" in rest else 0.0
        _label_fully_in_frame = bool(
            _bounds[0] >= -_wire_label_tolerance
            and _bounds[1] >= -_wire_label_tolerance
            and _bounds[2] <= 1.0 + _wire_label_tolerance
            and _bounds[3] <= 1.0 + _wire_label_tolerance)
        if not _projection.get("in_view") and not _intersects_frame:
            _text += " — OFF-SCREEN"
        elif not _label_fully_in_frame:
            _text += " — CLIPPED"
        _bbox = _draw.textbbox((0, 0), _text, font=_font, stroke_width=1)
        _tw = _bbox[2] - _bbox[0]
        _th = _bbox[3] - _bbox[1]
        _pad_x, _pad_y = 10, 6
        _label_w, _label_h = _tw + 2 * _pad_x, _th + 2 * _pad_y
        _box_center_y = int(max(
            0, min(_height - 1, (1.0 - _center[1]) * _height)))
        _positions = [
            (_box_x - _label_w // 2, _box_top - _label_h - 12),
            (_box_x - _label_w // 2, _box_center_y + 12),
            (_box_x + 14, _box_top),
            (_box_x - _label_w - 14, _box_top),
        ]
        _chosen = None
        for _candidate_left, _candidate_top in _positions:
            _candidate_left = max(
                4, min(_width - _label_w - 4, _candidate_left))
            _candidate_top = max(
                4, min(_height - _label_h - 4, _candidate_top))
            _candidate_rect = (
                _candidate_left, _candidate_top,
                _candidate_left + _label_w, _candidate_top + _label_h)
            if not any(
                    not (_candidate_rect[2] < other[0]
                         or _candidate_rect[0] > other[2]
                         or _candidate_rect[3] < other[1]
                         or _candidate_rect[1] > other[3])
                    for other in _occupied_labels):
                _chosen = _candidate_rect
                break
        if _chosen is None:
            _candidate_left, _candidate_top = _positions[0]
            _candidate_left = max(
                4, min(_width - _label_w - 4, _candidate_left))
            _candidate_top = max(
                4, min(_height - _label_h - 4, _candidate_top))
            _chosen = (
                _candidate_left, _candidate_top,
                _candidate_left + _label_w, _candidate_top + _label_h)
        _occupied_labels.append(_chosen)
        _left, _top, _right, _bottom = _chosen
        _fill = tuple(int(channel * 255) for channel in _rgb) + (235,)
        _luma = 0.2126 * _rgb[0] + 0.7152 * _rgb[1] + 0.0722 * _rgb[2]
        _text_fill = (15, 15, 18, 255) if _luma > 0.55 else (255, 255, 255, 255)
        _draw.line(
            [(_box_x, _box_top), ((_left + _right) // 2, _bottom)],
            fill=(10, 10, 12, 255), width=5,
        )
        _draw.line(
            [(_box_x, _box_top), ((_left + _right) // 2, _bottom)],
            fill=_fill, width=3,
        )
        if _is_candidate_pin:
            _pin_radius = 9
            _draw.rounded_rectangle(
                [_box_x - _pin_radius, _box_top - _pin_radius,
                 _box_x + _pin_radius, _box_top + _pin_radius],
                radius=4, fill=_fill, outline=(8, 8, 10, 255), width=3,
            )
        _draw.rounded_rectangle(
            [_left, _top, _right, _bottom], radius=7,
            fill=_fill, outline=(8, 8, 10, 255), width=3,
        )
        _draw.text(
            (_left + _pad_x, _top + _pad_y), _text, font=_font,
            fill=_text_fill, stroke_width=1,
            stroke_fill=(255, 255, 255, 180) if _luma <= 0.55 else (0, 0, 0, 120),
        )
    _image.convert("RGB").save(OUT_PNG, format="PNG")
    print(
        f"[render] 已標示 {len(_annotation_requested_map)} 個 requested placeholder",
        flush=True)
print("RENDER_DONE", OUT_PNG, flush=True)
