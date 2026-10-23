"""Render one still per camera angle so you can pick a framing before
committing to a full video.

Renders 5 deliberately different views of the same scene and writes a contact
sheet next to the stills, so they can be compared side by side.

Usage:
  python preview_angles.py <scene.blend> <out_dir> [--samples 32]
                           [--res 800x450] [--angles ground,low,...]

Then feed the winner to render_video.py, e.g.
  python render_video.py scene.blend frames --angle low --motion dolly
"""

import argparse
import json
import math
import re
import statistics
import sys
from pathlib import Path

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "stage_exec"))

PH_COLL_RE = re.compile(r"placeholders:((.*)\((\d*)\))")


def inview_stats(scene, cam):
    """★這個角度真的看得到什麼、多遠★ —— 給 photographer agent 當判準。

    ★為什麼要有數字★：2026-08-09 第一次跑 photographer，agent 說某個角度
      「shows the most distinct placeholder clusters」，而那個場景近處★一個 cluster
      都沒有★。它把地形的岩層紋理讀成了結構，給了聽起來合理但沒有證據的理由。
    ⇒ 「看得到幾個、多近」是可以量的，就不要讓它用猜的。★圖給空間感，數字下判斷。★

    ⚠️ ★第一版只判視錐、不判遮擋 —— 數字虛胖 45 倍★
       snowy_mountain 報 in_view=58，但發光色只佔畫面 0.198%(2.2m 的 box 在 116m
       處該有 28px 寬、58 個該佔 ~9%)。那些 boulder 絕大多數★在雪脊後面★。
       agent 是照著虛胖的數字做決定的。
    ⚠️ ★遮擋測試不能只打一條射線到原點★ —— 今天已經證明那對孔雀太粗(把明明看得到
       的判成被擋)。改成取 bbox 的角點 + 中心共數點，★任一點沒被擋就算看得到★。
    """
    deps = bpy.context.evaluated_depsgraph_get()
    campos = cam.evaluated_get(deps).matrix_world.translation.copy()
    per = {}
    for coll in bpy.data.collections:
        m = PH_COLL_RE.fullmatch(coll.name)
        if not m:
            continue
        fac = m.group(2)
        rec = per.setdefault(fac, {"total": 0, "frustum": 0, "visible": 0, "dists": []})
        for o in coll.objects:
            if o.parent is not None:
                continue
            rec["total"] += 1
            # 取樣點：中心 + 8 個 bbox 角
            pts = [o.matrix_world.translation.copy()]
            pts += [o.matrix_world @ Vector(c) for c in o.bound_box]
            in_frustum = False
            seen = False
            for p in pts:
                ndc = world_to_camera_view(scene, cam, p)
                if not (0.0 <= ndc.x <= 1.0 and 0.0 <= ndc.y <= 1.0 and ndc.z > 0):
                    continue
                in_frustum = True
                d = (p - campos).length
                dirv = (p - campos).normalized()
                hit, loc, _n, _i, hobj, _mx = scene.ray_cast(
                    deps, campos + dirv * 0.05, dirv)
                # 命中自己、或命中點不比取樣點近 ⇒ 這個點看得到
                if (not hit) or (hobj is o) or ((loc - campos).length >= d - 0.3):
                    seen = True
                    break
            if in_frustum:
                rec["frustum"] += 1
            if seen:
                rec["visible"] += 1
                rec["dists"].append((o.matrix_world.translation - campos).length)
    out = {}
    for fac, rec in per.items():
        d = sorted(rec["dists"])
        out[fac] = {"total": rec["total"], "in_frustum": rec["frustum"],
                    "visible": rec["visible"],
                    "nearest": round(d[0], 1) if d else None,
                    "median": round(statistics.median(d), 1) if d else None}
    return out

# Each angle varies height (aerial), tilt below horizon (pitch), distance along
# the view axis (back, negative = push in), lateral offset (side) and turn (yaw).
#
# IMPORTANT -- keep these nudges SMALL.
#
# SphericalMesher builds terrain detail relative to the camera pose it sees at
# fine_terrain time, in three tiers: in-view visible 0.53 marching cube/pixel,
# in-view occluded 0.13, and everything out of view 0.07 -- eight times
# coarser. Swing the camera far enough and that coarse shell rotates into
# frame as visible flat triangles (worst on open scenes like coast, hidden by
# trees in forest).
#
# So every angle here stays close to Infinigen's own framing: modest height changes,
# small yaw, and no large lateral moves. Variety comes from tilt and framing,
# not from flying somewhere else.
ANGLES = {
    # Infinigen's own pose, untouched.
    "ground": dict(aerial=0.0, pitch=0.0, back=0.0, side=0.0, yaw=0.0,
                   note="scene's own camera, unmodified"),
    # Same spot, pushed slightly in and tilted a touch down: detail shot.
    "closeup": dict(aerial=0.5, pitch=4.0, back=-4.0, side=0.0, yaw=0.0,
                    note="pushed in 4m -- foreground detail"),
    # Small step back and a gentle turn: same area, wider read.
    "wide": dict(aerial=1.5, pitch=6.0, back=8.0, side=0.0, yaw=-12.0,
                 note="stepped back 8m, turned 12 deg"),
    # Lifted just above the undergrowth, still essentially level.
    "eye": dict(aerial=4.0, pitch=8.0, back=3.0, side=6.0, yaw=10.0,
                note="4m up, near level -- above the undergrowth"),
    # Highest of the set but still well inside the meshed region.
    "raised": dict(aerial=9.0, pitch=16.0, back=6.0, side=-6.0, yaw=-8.0,
                   note="9m up, mild downward tilt"),
}


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else sys.argv[1:]
    ap = argparse.ArgumentParser()
    ap.add_argument("blend")
    ap.add_argument("out_dir")
    ap.add_argument("--samples", type=int, default=32)
    ap.add_argument("--res", default="800x450")
    ap.add_argument("--angles", default=",".join(ANGLES),
                    help="comma-separated subset of: " + ", ".join(ANGLES))
    ap.add_argument("--frame", type=int, default=1, help="which frame to sample")
    ap.add_argument("--show-placeholders", action="store_true",
                    help="coarse blends only: unhide the placeholder boxes so the "
                         "frame shows scene STRUCTURE (nothing is populated yet)")
    ap.add_argument("--color", action="store_true",
                    help="emission-colour each placeholder by factory + write a legend. "
                         "Without it the boxes are grey and read as terrain.")
    ap.add_argument("--view", default=None,
                    help="view transform, e.g. Standard. ★上色的 box 一定要用 "
                         "Standard + --exp★ —— 場景預設的 AgX 會把發光色去飽和到看不見 "
                         "(planner/run_all.sh:65 那組成功的就是 Standard/-1.5)")
    ap.add_argument("--exp", type=float, default=None, help="view exposure")
    ap.add_argument("--marker-box", action="store_true",
                    help="★一律把 placeholder 換成 bbox 尺寸的方塊★ —— placeholder 的形狀"
                         "是每個 factory 自己決定的(樹=spawn_cube(4)、boulder=50 頂點低模"
                         "石頭)，不統一的話 agent 會把石頭形 placeholder 讀成地形景物")
    ap.add_argument("--wire", action="store_true",
                    help="marker box 改成線框 —— 標示位置與大小，同時不遮擋後面的地形"
                         "(agent 還要判構圖與遮擋)")
    ap.add_argument("--real-size", action="store_true",
                    help="shrink boxes to the asset's real size (default 4x4x4 "
                         "overstates small things and understates trees)")
    return ap.parse_args(argv)


def unhide_placeholders(scene):
    """★coarse blend 專用★：打開 placeholder collection 的 hide_render。

    ⚠️ ★坑(0726)★：只翻 collection 層級旗標，★不要走訪 coll.all_objects★ ——
       帶 GN 的物件逐個走訪會觸發求值 → segfault(render_view.py 有同樣的註記)。
    ⚠️ base_nature.gin 的 group_collections 預設把 placeholders 設成 hide_render=True，
       所以 coarse blend 不打開就是★渲出一片空地★。
    """
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
    print(f"opened {n} placeholder collections", flush=True)
    return n


def terrain_objs():
    """會被渲染的地形表面。★不要寫死名字★(0803 血淚，見 placement_modes)。"""
    out = []
    for o in bpy.data.objects:
        if o.type != "MESH" or not o.name.startswith("OpaqueTerrain"):
            continue
        if "collider" in o.name:
            continue
        out.append(o)
    return out


def ground_stats(scene, cam, terr, nx=14, ny=9):
    """★這個角度有多少「可以放東西」的地面★ —— 選相機的主判準。

    ★為什麼這才是主判準★(2026-08-09，使用者指出)：
      「看得到幾個既有 placeholder」是 ★replace 的判準★(要換掉既有的樹時才重要)。
      但 xmas 要的是「在雪山上★加★聖誕樹」—— 167 個 boulder 在不在畫面裡，跟能不能
      放下 4 棵樹沒有因果關係。真正要問的是：★鏡頭前有沒有可用的地面★
      (夠近、沒被山脊擋、坡度不太陡、不是一片天空)。
      那是「地形 × 視角」的性質，★與場景裡有什麼無關★，所以任何場景都適用。

    做法：在 NDC 上撒格點，每點往地形打射線(★只打地形★，其餘物件不算遮擋 ——
      coarse 的 placeholder 不是最終幾何，0809 已經證明拿它當牆會全滅)。
    """
    deps = bpy.context.evaluated_depsgraph_get()
    mw = cam.evaluated_get(deps).matrix_world
    campos = mw.translation.copy()
    fwd = -Vector((mw[0][2], mw[1][2], mw[2][2])).normalized()
    right = Vector((mw[0][0], mw[1][0], mw[2][0])).normalized()
    up = Vector((mw[0][1], mw[1][1], mw[2][1])).normalized()
    tan_x, tan_y = math.tan(cam.data.angle_x / 2), math.tan(cam.data.angle_y / 2)

    hits, dists, slopes = 0, [], []
    total = 0
    for iy in range(ny):
        for ix in range(nx):
            # 邊緣留白：物件有體積，中心壓在邊界會被切一半
            u = 0.10 + 0.80 * (ix + 0.5) / nx
            v = 0.10 + 0.60 * (iy + 0.5) / ny
            total += 1
            d = (right * ((u * 2 - 1) * tan_x)
                 + up * ((v * 2 - 1) * tan_y) + fwd).normalized()
            best = None
            for t in terr:
                ev = t.evaluated_get(deps)
                mwi = t.matrix_world.inverted()
                try:
                    ok, loc_l, nrm_l, _i = ev.ray_cast(
                        mwi @ campos, (mwi.to_3x3() @ d).normalized(), distance=1e5)
                except Exception:
                    continue
                if not ok:
                    continue
                loc_w = t.matrix_world @ loc_l
                dist = (loc_w - campos).length
                if best is None or dist < best[0]:
                    nrm_w = (t.matrix_world.to_3x3() @ nrm_l).normalized()
                    best = (dist, nrm_w)
            if best is None:
                continue                       # 打到天空
            hits += 1
            dists.append(best[0])
            slopes.append(math.degrees(math.acos(max(-1.0, min(1.0, best[1].z)))))
    if not dists:
        return {"ground_frac": 0.0, "usable_near": 0.0, "flat_frac": 0.0,
                "nearest_ground": None, "median_ground": None}
    near = [d for d in dists if 5.0 <= d <= 40.0]
    flat = [s for s, d in zip(slopes, dists) if s < 25.0 and 5.0 <= d <= 40.0]
    return {"ground_frac": round(hits / total, 3),
            "usable_near": round(len(near) / total, 3),
            "flat_frac": round(len(flat) / max(1, len(near)), 3),
            "nearest_ground": round(min(dists), 1),
            "median_ground": round(statistics.median(dists), 1)}


def pick_camera(scene):
    if scene.camera is not None:
        return scene.camera
    cams = [o for o in scene.objects if o.type == "CAMERA"]
    if not cams:
        raise SystemExit("no camera in blend")
    scene.camera = cams[0]
    return cams[0]


def setup_gpu(scene):
    scene.render.engine = "CYCLES"
    scene.cycles.use_denoising = True
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        prefs.compute_device_type = "OPTIX"
        prefs.get_devices()
        for d in prefs.devices:
            d.use = d.type != "CPU"
        scene.cycles.device = "GPU"
    except Exception as e:  # noqa: BLE001
        print("GPU setup warning:", e)


def main():
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    bpy.ops.wm.open_mainfile(filepath=args.blend)
    scene = bpy.context.scene
    cam = pick_camera(scene)

    rig = cam
    while rig.parent is not None:
        rig = rig.parent

    # Record the untouched pose so every angle starts from the same place.
    base_loc = rig.location.copy()
    cam_constraints = [(c.type, c.name) for c in cam.constraints]
    base_cam_rot = cam.rotation_euler.copy()

    w, h = (int(x) for x in args.res.lower().split("x"))
    scene.render.resolution_x = w
    scene.render.resolution_y = h
    scene.render.resolution_percentage = 100
    scene.cycles.samples = args.samples
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    setup_gpu(scene)
    if args.view:
        scene.view_settings.view_transform = args.view
    if args.exp is not None:
        scene.view_settings.exposure = args.exp
    scene.frame_set(args.frame)
    legend = []
    if args.show_placeholders:
        unhide_placeholders(scene)
        if args.real_size or args.color or args.marker_box:
            import placeholder_colors as pc
            # ⚠️ 順序：先換幾何再上色 —— 上色只碰 root mesh，換幾何會蓋掉材質指定。
            if args.marker_box:
                pc.marker_boxes(wire=args.wire)
            elif args.real_size:
                pc.rescale_to_real_size(wire=args.wire)
            if args.color:
                stats = pc.colorize_placeholders()
                legend = pc.legend_lines(stats)
                print("LEGEND " + json.dumps(legend), flush=True)

    wanted = [a.strip() for a in args.angles.split(",") if a.strip()]
    unknown = [a for a in wanted if a not in ANGLES]
    if unknown:
        raise SystemExit(f"unknown angle(s): {unknown}; choose from {list(ANGLES)}")

    all_stats = {}
    for name in wanted:
        cfg = ANGLES[name]
        # Reset to the original pose before applying this angle.
        rig.location = base_loc.copy()
        cam.rotation_euler = base_cam_rot.copy()

        if any(cfg[k] for k in ("aerial", "back", "side", "yaw")):
            # The rig carries constraints and animated rotation; clear both so
            # our tilt survives, then keyframe it (frame_set restores unkeyed
            # values from the blend otherwise).
            for c in list(cam.constraints):
                cam.constraints.remove(c)
            for c in list(rig.constraints):
                rig.constraints.remove(c)
            cam.animation_data_clear()
            rig.animation_data_clear()
            rig.location.z += cfg["aerial"]
            # Pitch tilts down from the horizon; yaw turns the camera to face a
            # different part of the scene (without it, moving sideways just
            # shows the same view shifted).
            cam.rotation_mode = "XYZ"
            cam.rotation_euler = (
                math.radians(90.0 - cfg["pitch"]),
                0.0,
                base_cam_rot.z + math.radians(cfg["yaw"]),
            )
            rig.rotation_mode = "XYZ"
            rig.rotation_euler = (0.0, 0.0, rig.rotation_euler.z)

            # Move along the re-aimed basis. Update first so the forward/right
            # vectors reflect the new tilt and yaw.
            if cfg["back"] or cfg["side"]:
                bpy.context.view_layer.update()
                deps = bpy.context.evaluated_depsgraph_get()
                m = cam.evaluated_get(deps).matrix_world
                fwd = -Vector((m[0][2], m[1][2], m[2][2])).normalized()
                right = Vector((m[0][0], m[1][0], m[2][0])).normalized()
                rig.location -= fwd * cfg["back"]
                rig.location += right * cfg["side"]

        bpy.context.view_layer.update()
        deps = bpy.context.evaluated_depsgraph_get()
        pos = cam.evaluated_get(deps).matrix_world.translation

        # ★在渲染之前算 in-view★ —— 相機此刻就是這個 angle 的姿態
        st = inview_stats(scene, cam)
        gs = ground_stats(scene, cam, terrain_objs())
        all_stats[name] = {"cam": [round(pos.x, 1), round(pos.y, 1), round(pos.z, 1)],
                           "cfg": {k: v for k, v in cfg.items() if k != "note"},
                           "by_factory": st, "ground": gs,
                           "visible_total": sum(v["visible"] for v in st.values()),
                           "frustum_total": sum(v["in_frustum"] for v in st.values()),
                           "nearest": min([v["nearest"] for v in st.values()
                                           if v["nearest"] is not None], default=None)}
        print(f"STATS {name} " + json.dumps(all_stats[name]), flush=True)

        scene.render.filepath = str(out_dir / f"angle_{name}")
        bpy.ops.render.render(write_still=True)
        print(
            f"ANGLE {name}: aerial={cfg['aerial']}m pitch={cfg['pitch']}deg "
            f"back={cfg['back']}m side={cfg['side']}m yaw={cfg['yaw']}deg cam=({pos.x:.1f},{pos.y:.1f},{pos.z:.1f}) "
            f"-- {cfg['note']}",
            flush=True,
        )

    (out_dir / "angle_stats.json").write_text(
        json.dumps({"legend": legend, "angles": all_stats}, indent=2,
                   ensure_ascii=False) + "\n", encoding="utf-8")
    print("PREVIEW_DONE", out_dir)


if __name__ == "__main__":
    main()
