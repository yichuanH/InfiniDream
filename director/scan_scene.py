r"""
scan_scene —— Director 的確定性前置：掃一個跑好的 Infinigen blend，列出
「這個場景裡實際有什麼可操作的 asset」。

★這不是猜的、不是看圖認的★——直接讀 placeholder collection 名，
   r"placeholders:(\w+Factory)\((\d+)\)"，所以 factory 類名精確、不漏不錯。
   (0724 第十節決策2：detect 清單是 planner 的地基，單靠 LLM 看圖認不出類名)

在 bpy 環境跑(獨立子進程)，輸出 scene_manifest.json 給 director.py 讀。

  python scan_scene.py -- <blend> <out_json>

輸出 schema:
{
  "blend": "...",
  "camera": {"location": [x,y,z], "rotation_euler": [...]},
  "factories": [
    {"factory": "TreeFactory",          # placeholder collection 名解出的類名
     "seeds": [0, 1],                   # 出現過的 factory_seed
     "total": 685,                      # 全場景 placeholder 數
     "in_view": 55,                     # 目前相機視錐內的數量
     "dist_min": 12.3, "dist_max": 180.4, "dist_median": 61.2,   # 對相機距離(全部)
     "nearest_in_view": 12.3},
    ...
  ]
}

★算一次兩處用★(0724 第六節/第十節)：
  - 餵 director → 它才知道「場景有什麼可以 replace / delete」
  - 餵 replacer(composer.py) → distance / 可見集合
"""
import bpy, sys, re, json, statistics, math
from pathlib import Path
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scatter_catalog as sc
import surface_registry as sr

argv = sys.argv[sys.argv.index("--") + 1:]
BLEND = argv[0]
OUT_JSON = argv[1]
SCENE_NAME = argv[argv.index("--scene") + 1] if "--scene" in argv else ""
CANONICAL_SCENE_NAME = sr.canonical_scene_name(SCENE_NAME)
INDOOR_SCENE_NAMES = {
    "bedroom", "living-room", "kitchen", "bathroom", "dining-room"}

# 與 composer.py L108 同一條 pattern —— 兩邊必須一致，否則 director 說有、replacer 找不到
PH_COLL_RE = re.compile(r"placeholders:(\w+Factory)\((\d+)\)")
PH_OBJ_RE = re.compile(r"spawn_placeholder\((\d+)\)")

print(f"[scan] opening {BLEND}", flush=True)
bpy.ops.wm.open_mainfile(filepath=BLEND)
scene = bpy.context.scene
cam = scene.camera or next((o for o in scene.objects if o.type == "CAMERA"), None)
if cam is None:
    raise SystemExit("[scan] 這個 blend 沒有相機")
scene.camera = cam
scene.frame_set(1)
bpy.context.view_layer.update()
campos = cam.evaluated_get(bpy.context.evaluated_depsgraph_get()).matrix_world.translation

# ★用 blend 的原生相機★(不套 composer 的 raised 視角)——manifest 描述的是「場景現況」，
#   視角調整是 replacer/render 階段的事，這裡不能先套，否則 in_view 與 render 圖對不上。

agg = {}   # factory_name -> {"seeds": set, "dists": [], "in_view_dists": []}
per_coll = []   # ★每個 collection 一筆★ —— per-collection 替換需要各自的數量
for coll in bpy.data.collections:
    m = PH_COLL_RE.match(coll.name)
    if not m:
        continue
    fac_name, seed = m.group(1), int(m.group(2))
    a = agg.setdefault(fac_name, {"seeds": set(), "dists": [], "in_view_dists": []})
    a["seeds"].add(seed)
    c_tot, c_inview, c_inview_d = 0, 0, []
    for o in coll.all_objects:
        if not PH_OBJ_RE.search(o.name):
            continue
        d = (o.location - campos).length
        a["dists"].append(d)
        c_tot += 1
        co = world_to_camera_view(scene, cam, o.location)
        if 0 < co.x < 1 and 0 < co.y < 1 and co.z > 0:
            a["in_view_dists"].append(d)
            c_inview += 1
            c_inview_d.append(d)
    # ★per-collection 的距離★(2026-07-29 補)：
    #   in_view 只說「在視錐裡」，★沒說「看得見」★。
    #   desert 實測：CactusFactory in_view=19 但 nearest=36m ⇒ 畫面上只是遠處小點，
    #   ★換成玻璃仙人掌 render 幾乎看不出差異★。這比 0728 6-E 的 view cull 更隱蔽。
    #   ⇒ planner 要能分辨「前景可改」與「太遠改了也沒用」。
    per_coll.append({
        "collection": coll.name, "factory": fac_name, "seed": seed,
        "total": c_tot, "in_view": c_inview,
        "nearest_in_view": round(min(c_inview_d), 1) if c_inview_d else None,
        "median_in_view": round(statistics.median(c_inview_d), 1) if c_inview_d else None,
    })

factories = []
for name, a in sorted(agg.items(), key=lambda kv: -len(kv[1]["dists"])):
    ds = a["dists"]
    factories.append({
        "factory": name,
        "seeds": sorted(a["seeds"]),
        "total": len(ds),
        "in_view": len(a["in_view_dists"]),
        "dist_min": round(min(ds), 2) if ds else None,
        "dist_max": round(max(ds), 2) if ds else None,
        "dist_median": round(statistics.median(ds), 2) if ds else None,
        "nearest_in_view": round(min(a["in_view_dists"]), 2) if a["in_view_dists"] else None,
    })
per_coll.sort(key=lambda c: (-c["in_view"], -c["total"]))

# ★boid 群(魚群/蟲群)★(2026-09-11)：coarse 就 populate 掉了，placeholders:X 是空的，
#   上面的迴圈看不到 ⇒ Director 以為場景沒有魚群，只能 hero add 3-5 條(0910 coral_reef)。
#   emitter 在 unique_assets:X，群內個體是 particleassets:X.children 的母本被 instance。
swarms = []
for pcol in bpy.data.collections:
    m = re.fullmatch(r"particleassets:(([A-Za-z_0-9]+)\((\d+)\))\.children", pcol.name)
    if not m:
        continue
    key, fac_name, seed = m.group(1), m.group(2), int(m.group(3))
    ucol = bpy.data.collections.get(f"unique_assets:{key}")
    emitters = [o for o in (ucol.objects if ucol else [])
                if getattr(o, "particle_systems", None) and len(o.particle_systems)]
    if not emitters:
        continue
    em = emitters[0]
    # ⚠️ ★不能用 matrix_world★：unique_assets 的 collection 是 hide_viewport=True，
    #    depsgraph 不求值它 ⇒ matrix_world 恆為 (0,0,0)(0911 coral_reef 三群全讀成
    #    79m)。emitter 沿 animate_trajectory 的 location 關鍵影格移動、沒有 parent，
    #    所以直接取第 1 格的 fcurve 值。
    loc = em.location.copy()
    act = em.animation_data.action if em.animation_data else None
    for fc in (act.fcurves if act else []):
        if fc.data_path == "location" and 0 <= fc.array_index < 3:
            loc[fc.array_index] = fc.evaluate(scene.frame_current)
    co = world_to_camera_view(scene, cam, loc)
    swarms.append({
        "target": key,                       # replace 時 target_factory 填這個
        "factory": fac_name, "seed": seed,
        "kind": "fish_school" if "Fish" in fac_name else "bug_swarm",
        "collection": ucol.name, "master_collection": pcol.name,
        "n_masters": len(pcol.objects),
        "particle_count": int(em.particle_systems[0].settings.count),
        "emitter_location": [round(float(v), 2) for v in loc],
        "distance_m": round((loc - campos).length, 1),
        "center_in_view": bool(0 < co.x < 1 and 0 < co.y < 1 and co.z > 0),
        # 魚群會游動、粒子散在約 4m 的體積裡，所以中心稍微出框仍然看得到一部分
        "near_frame": bool(co.z > 0 and -0.15 < co.x < 1.15 and -0.15 < co.y < 1.15),
        "screen_position": [round(float(co.x), 4), round(float(co.y), 4)],
    })
swarms.sort(key=lambda s: s["distance_m"])

# ═══════════════════════════════════════════════════════════════════════
# ★EXTRA / ground scatter★(2026-07-29 補：先前完全沒掃，planner 只能靠猜，
#   於是產出「assumed present」「hero fallback」這種★條件式 plan★，
#   而下游 composer 要的是確定的 JSON，吃不下條件句。)
#
# ★形式(0728 7-A 實測)★：scatter:<名字> 物件，★1 個頂點 + GN modifier★
#   位置★不存在 blend 裡★ —— GN 在 render 時即時算，所以沒有 total/in_view 可報。
#   兩個可編輯點：Overall Density(float) / COLLECTION_INFO(母本集合指標)
# ═══════════════════════════════════════════════════════════════════════
scatters = []
for o in bpy.data.objects:
    if not o.name.startswith("scatter:"):
        continue
    gn = [m for m in o.modifiers if m.type == "NODES"]
    explicit_id = sc.canonical_semantic_id(o.get(sc.SEMANTIC_ID_PROPERTY))
    semantic_id = explicit_id or sc.semantic_id_from_carrier_name(o.name)
    entry = {
        "name": o.name,
        "label": o.name[len("scatter:"):],
        "semantic_id": semantic_id,
        "semantic_id_evidence": (
            "custom_property" if explicit_id else
            "anchored_legacy_name" if semantic_id else "unresolved"
        ),
        "density": None,
        "masters": None,
        "master_collection": None,
    }
    for m in gn:
        if not m.node_group:
            continue
        # ★Overall Density★：modifier 的 input socket，用 interface 的 identifier 取值
        try:
            for item in m.node_group.interface.items_tree:
                if getattr(item, "name", "") == "Overall Density":
                    entry["density"] = round(float(m[item.identifier]), 3)
        except Exception:
            pass
        # COLLECTION_INFO 指到的母本集合 = 這批東西「長什麼樣」的來源
        for n in m.node_group.nodes:
            if n.type == "COLLECTION_INFO" and n.inputs["Collection"].default_value:
                col = n.inputs["Collection"].default_value
                entry["master_collection"] = col.name
                entry["masters"] = len(col.objects)
    scatters.append(entry)
scatters.sort(key=lambda s: s["label"])

# Exact allow-list for Agent-authored surface policies. Keep this filter in
# lockstep with apply_plan.visible_surface_hit: render-visible, non-placeholder
# meshes only. Names are factual identifiers, not semantic classifications.
raycast_surfaces = []
surface_cache = []
depsgraph = bpy.context.evaluated_depsgraph_get()
for o in bpy.data.objects:
    if o.type != "MESH" or o.hide_render or "collider" in o.name.lower():
        continue
    if any(PH_COLL_RE.fullmatch(c.name) for c in o.users_collection):
        continue
    evaluated = o.evaluated_get(depsgraph)
    mesh = getattr(evaluated, "data", None)
    if mesh is None or not hasattr(mesh, "vertices") or len(mesh.vertices) == 0:
        continue
    matrix = evaluated.matrix_world.copy()
    try:
        inverse = matrix.inverted()
        evaluated.ray_cast(
            Vector((0.0, 0.0, 0.0)),
            Vector((0.0, 0.0, 1.0)),
            distance=1e-6,
        )
    except (AttributeError, RuntimeError, ValueError, ReferenceError):
        continue
    surface_types = sr.types_for_mesh(SCENE_NAME, o.name)
    hint = ("explicit_liquid" if re.fullmatch(
        r"liquid(?:\.\d+)?", o.name, re.IGNORECASE) else
        "registry:" + ",".join(surface_types) if surface_types else
        "unclassified")
    raycast_surfaces.append({
        "name": o.name,
        "semantic_hint": hint,
        "semantic_types": surface_types,
        "raycast_ready": True,
        "policy_selectable": False,
    })
    surface_cache.append((o.name, evaluated, matrix, inverse))
raycast_surfaces.sort(key=lambda x: x["name"].casefold())

# Deterministically sample the active-camera image using the same nearest-hit
# visibility semantics as Step 8. A semantic type is available only if the first
# visible surface at a sampled screen point belongs to that registry type.
def _sample_axis(count: int, *, margin: float) -> tuple[float, ...]:
    if count <= 1:
        return (0.5,)
    step = (1.0 - 2.0 * margin) / float(count - 1)
    return tuple(round(margin + index * step, 6) for index in range(count))


is_indoor_scene = CANONICAL_SCENE_NAME in INDOOR_SCENE_NAMES
sample_x = (_sample_axis(17, margin=0.04) if is_indoor_scene
            else (0.05, 0.2, 0.35, 0.5, 0.65, 0.8, 0.95))
sample_y = (_sample_axis(10, margin=0.04) if is_indoor_scene
            else (0.05, 0.23, 0.41, 0.59, 0.77, 0.95))
cam_world = cam.evaluated_get(depsgraph).matrix_world.copy()
origin = cam_world.translation.copy()
forward = -Vector((cam_world[0][2], cam_world[1][2], cam_world[2][2])).normalized()
right = Vector((cam_world[0][0], cam_world[1][0], cam_world[2][0])).normalized()
up = Vector((cam_world[0][1], cam_world[1][1], cam_world[2][1])).normalized()
tan_x, tan_y = math.tan(cam.data.angle_x / 2), math.tan(cam.data.angle_y / 2)
visible_registry_meshes = set()
visible_surface_samples = []
for nx in sample_x:
    for ny in sample_y:
        direction = (right * ((nx * 2 - 1) * tan_x)
                     + up * ((ny * 2 - 1) * tan_y) + forward).normalized()
        best = None
        for name, ev, matrix, inverse in surface_cache:
            try:
                ok, loc_l, normal_l, _face = ev.ray_cast(
                    inverse @ origin,
                    (inverse.to_3x3() @ direction).normalized(), distance=1e5)
            except (AttributeError, RuntimeError, ValueError, ReferenceError):
                continue
            if not ok:
                continue
            loc_w = matrix @ loc_l
            distance = (loc_w - origin).length
            if best is None or distance < best[0]:
                normal_w = (
                    matrix.to_3x3().inverted().transposed() @ normal_l
                ).normalized()
                best = (distance, name, loc_w, normal_w)
        if best is None:
            continue
        surface_types = sr.types_for_mesh(SCENE_NAME, best[1])
        if not surface_types:
            continue
        visible_registry_meshes.add(best[1])
        visible_surface_samples.append({
            "screen": [round(nx, 6), round(ny, 6)],
            "world": [round(float(value), 5) for value in best[2]],
            "normal": [round(float(value), 5) for value in best[3]],
            "distance": round(float(best[0]), 5),
            "mesh_name": best[1],
            "surface_types": surface_types,
            "evidence": "nearest_camera_raycast+mesh_level_registry",
        })
available_surface_types = sr.resolve_available(
    SCENE_NAME, visible_registry_meshes)
visible_registry_meshes_casefold = {
    name.casefold() for name in visible_registry_meshes}
for row in raycast_surfaces:
    row["policy_selectable"] = (
        row["name"].casefold() in visible_registry_meshes_casefold
        or row["semantic_hint"] == "explicit_liquid"
    )

manifest = {
    "blend": str(BLEND),
    "camera": {
        "location": [round(v, 3) for v in campos],
        "rotation_euler": [round(v, 4) for v in cam.rotation_euler],
    },
    "factories": factories,
    "collections": per_coll,   # ★每個 placeholder collection 各自的數量★
    "scatters": scatters,      # ★EXTRA / ground scatter★
    "swarms": swarms,          # ★coarse 就 populate 的 boid 群(魚群/蟲群)★
    "raycast_surfaces": raycast_surfaces,
    "visible_surface_samples": visible_surface_samples,
    "surface_registry_version": 1,
    "scene_type": SCENE_NAME,
    "canonical_scene_type": CANONICAL_SCENE_NAME,
    "surface_inventory_sampling": {
        "columns": len(sample_x),
        "rows": len(sample_y),
        "nearest_hit": True,
    },
    "available_surface_types": available_surface_types,
}
Path(OUT_JSON).parent.mkdir(parents=True, exist_ok=True)
Path(OUT_JSON).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

print(f"[scan] 找到 {len(factories)} 種 factory / {len(per_coll)} 個 collection:", flush=True)
for f in factories:
    print(f"[scan]   {f['factory']:28} total={f['total']:5}  in_view={f['in_view']:4}  "
          f"seeds={f['seeds']}  nearest_in_view={f['nearest_in_view']}", flush=True)
print(f"[scan] 每個 collection:", flush=True)
for c in per_coll:
    print(f"[scan]   {c['collection']:46} total={c['total']:5}  in_view={c['in_view']:4}",
          flush=True)
print(f"[scan] EXTRA / ground scatter: {len(scatters)} 個", flush=True)
for s in scatters:
    print(f"[scan]   {s['label']:36} density={s['density']}  "
          f"masters={s['masters']} ({s['master_collection']})", flush=True)
print(f"[scan] raycast surfaces: {[x['name'] for x in raycast_surfaces]}", flush=True)
print(f"[scan] available surface types: "
      f"{[x['type'] for x in available_surface_types]}", flush=True)
print("SCAN_DONE", OUT_JSON, flush=True)
