"""Render the validated scene animation without changing camera or particles.

Unlike the survey renderer, this script does not clear actions, free caches,
extend lifetimes, or create a new dolly.  The final-scene contract validates
the saved motion state; this renderer consumes that state as-is.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import bpy


def parse_args() -> argparse.Namespace:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else sys.argv[1:]
    parser = argparse.ArgumentParser()
    parser.add_argument("blend", type=Path)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--frame-start", type=int, required=True)
    parser.add_argument("--frame-end", type=int, required=True)
    parser.add_argument("--fps", type=int, default=12)
    parser.add_argument("--samples", type=int, default=64)
    parser.add_argument("--res", default="1280x720")
    parser.add_argument("--view", default="AgX")
    # 跟 render_view.py 的 still 一樣(預設 -1.5)，不然影片比 after.png 亮 1.5 檔、顏色偏淡(2026-09-12)
    parser.add_argument("--exp", type=float, default=-1.5)
    # 相機慢速位移(使用者 2026-09-12)：auto 依場景挑方向(有樹往上、河流/平原左右)，none 關掉
    #   方向可以用 "+" 疊(down+in)，逐場景倍率寫在 SCENE_MOVES 的 "*" 後面。
    parser.add_argument("--camera-move", default="none")
    # ★基準 0.8→1.6m★(2026-09-13 使用者：鏡頭移動都有點慢，全部快 1.25~2 倍)
    parser.add_argument("--camera-move-m", type=float, default=1.6)
    parser.add_argument("--scene", default="")
    return parser.parse_args(argv)


# 使用者 2026-09-12 逐場景指定的鏡頭運動；沒列到的用下面的通則。
#   2026-09-13 第三輪回饋後改：snowy_mountain 往下+推近、plain 推近、canyon/cliff 加速，
#   倍率寫在 "*" 後面(乘在 --camera-move-m 上)。推拉(in/out)另外再乘 IN_OUT_GAIN。
SCENE_MOVES = {
    "desert": "tilt_down*1.5", "arctic": "zoom",
    "snowy_mountain": "zoom", "snowy": "zoom",
    "river": "up*1.25", "coral_reef": "up*1.25", "coral": "up*1.25",
    "under_water": "left*1.25", "canyon": "up*1.5", "cliff": "up*1.5",
    "plain": "zoom", "coast": "right*1.25",
}
UP_SCENES = ("forest", "canyon", "mountain", "cliff", "coast", "cave", "japanese")
SIDE_SCENES = ("plain", "kelp")
IN_OUT_GAIN = 2.0          # 推拉要走比較多距離才看得出來
TILT_DEG = 7.0             # tilt_down/tilt_up 一整段轉幾度(繞相機自己的位置，不是平移)
ZOOM_GAIN = 0.35           # zoom*k：焦距從 lens 變成 lens×(1+ZOOM_GAIN×k)。只動相機鏡頭、不動變換 ⇒ 有 parent 的相機也安全


SINGLE_MOVES = ("none", "left", "right", "up", "down", "in", "out")


def _pick_camera_move(mode: str, scene: str, seed_text: str) -> str:
    if mode != "auto":
        return mode
    import hashlib
    name = scene.lower()
    for key, move in SCENE_MOVES.items():
        if key in name:
            return move
    h = int(hashlib.sha256(seed_text.encode("utf-8")).hexdigest()[:8], 16)
    if any(k in name for k in SIDE_SCENES):
        return "left" if h % 2 == 0 else "right"
    if any(k in name for k in UP_SCENES):
        return "up"
    return ("left", "right", "up")[h % 3]


def _apply_zoom(scene, cam, zoom: float) -> None:
    if abs(zoom - 1.0) <= 1e-6 or getattr(cam, "data", None) is None:
        return
    lens0 = float(cam.data.lens)
    cam.data.lens = lens0
    cam.data.keyframe_insert("lens", frame=scene.frame_start)
    cam.data.lens = lens0 * zoom
    cam.data.keyframe_insert("lens", frame=scene.frame_end)
    if cam.data.animation_data and cam.data.animation_data.action:
        for fc in cam.data.animation_data.action.fcurves:
            if fc.data_path == "lens":
                for kp in fc.keyframe_points:
                    kp.interpolation = "LINEAR"
    print(f"[animation] zoom 焦距 {lens0:.1f}mm → {lens0 * zoom:.1f}mm", flush=True)


def apply_camera_move(scene, direction: str, distance_m: float) -> str:
    """相機沿自己的 右/上/前 軸慢慢平移(整段幀範圍線性)。不存檔、不動場景其他東西。

    direction 可以是 "down"、"up*1.5"、"down+in"、"down*1.5+in" —— "+" 疊方向，
    "*" 是那個方向自己的倍率(2026-09-13 使用者要求鏡頭更快、雪山要往下又推近)。
    """
    from mathutils import Vector
    cam = scene.camera
    if not direction or direction == "none" or distance_m <= 0.0:
        return "none"
    import bpy
    scene.frame_set(scene.frame_start)
    m = cam.matrix_world
    right = Vector((m[0][0], m[1][0], m[2][0])).normalized()
    up = Vector((m[0][1], m[1][1], m[2][1])).normalized()
    forward = Vector((-m[0][2], -m[1][2], -m[2][2])).normalized()   # 相機看的方向
    axes = {"left": -right, "right": right, "up": up, "down": -up,
            "in": forward, "out": -forward}
    delta = Vector((0.0, 0.0, 0.0))
    tilt = 0.0
    zoom = 1.0
    for token in str(direction).split("+"):
        name, _, gain_text = token.strip().partition("*")
        name = name.strip()
        gain = float(gain_text) if gain_text.strip() else 1.0
        # ★zoom = 真的放大(改焦距)★(2026-09-13 使用者：相機改成放大)。in/out 是往前移動，會穿過前景。
        if name == "zoom":
            zoom *= 1.0 + ZOOM_GAIN * gain
            continue
        # ★tilt = 鏡頭「往下照」，不是往下平移★(2026-09-13 使用者：desert 相機改成往下照)
        if name in ("tilt_down", "tilt_up"):
            tilt += math.radians(TILT_DEG) * gain * (-1.0 if name == "tilt_down" else 1.0)
            continue
        if name not in axes:
            raise SystemExit(f"unknown camera move direction {name!r} in {direction!r}")
        if name in ("in", "out"):
            gain *= IN_OUT_GAIN
        delta += axes[name] * (distance_m * gain)
    import os
    if os.environ.get("BI_CAMERA_MOVE_LEGACY") != "1":
        # ★位移/俯仰一律在世界座標算★(2026-09-14 desert：「down*1.25」實測變成往前推 2m、完全沒往下)。
        #   舊寫法把世界座標的 delta 直接加在 rig.location，但 rig 掛在 camrig 底下(camrig 轉了 86°/-144°)
        #   ⇒ location 是 camrig 的區域座標 ⇒ up/down/left/right 全跑到別的軸；tilt 也因此被關掉。
        #   新寫法：逐幀求 rig 應有的世界矩陣(平移 delta×t、繞相機位置沿相機右軸轉 tilt×t)，換回 rig 區域座標打關鍵幀。
        #   只 frame_set 一次(camrig 本身不動，pipeline 相機是固定的)。
        #   BI_CAMERA_MOVE_LEGACY=1 走下面的舊寫法(before 影片要跟已經渲好的 after 一模一樣)。
        from mathutils import Matrix
        rig = bpy.data.objects.new("BI_CAMERA_MOVE_RIG", None)
        scene.collection.objects.link(rig)
        cam_pos = m.translation.copy()
        old_parent = cam.parent
        if old_parent is not None:
            rig.parent = old_parent
            rig.matrix_parent_inverse = cam.matrix_parent_inverse.copy()
            cam.matrix_parent_inverse.identity()
        cam.parent = rig
        bpy.context.view_layer.update()
        rig0 = rig.matrix_world.copy()
        parent_inv = (rig0 @ rig.matrix_basis.inverted()).inverted()
        start, end = int(scene.frame_start), int(scene.frame_end)
        prev = rig.rotation_euler.copy()
        for f in range(start, end + 1):
            t = (f - start) / max(1, end - start)
            world = (Matrix.Translation(delta * t) @ Matrix.Translation(cam_pos)
                     @ Matrix.Rotation(tilt * t, 4, right) @ Matrix.Translation(-cam_pos) @ rig0)
            loc, rot, _scale = (parent_inv @ world).decompose()
            rig.location = loc
            prev = rot.to_euler("XYZ", prev)
            rig.rotation_euler = prev
            rig.keyframe_insert("location", frame=f)
            rig.keyframe_insert("rotation_euler", frame=f)
        for fc in rig.animation_data.action.fcurves:
            for kp in fc.keyframe_points:
                kp.interpolation = "LINEAR"
        _apply_zoom(scene, cam, zoom)
        return (f"{direction} {delta.length:.2f}m"
                f"{f' + zoom ×{zoom:.2f}' if abs(zoom - 1.0) > 1e-6 else ''}"
                f"{f' + tilt {math.degrees(tilt):+.1f}°' if tilt else ''} (world rig)")
    # 相機本身的動畫(pipeline 固定相機也是關鍵幀)原封不動：掛到一個空物件底下、動空物件
    rig = bpy.data.objects.new("BI_CAMERA_MOVE_RIG", None)
    scene.collection.objects.link(rig)
    old_parent = cam.parent
    if old_parent is not None:
        rig.parent = old_parent
        rig.matrix_parent_inverse = cam.matrix_parent_inverse.copy()
        cam.matrix_parent_inverse.identity()
    cam_matrix = cam.matrix_world.copy()
    if tilt and old_parent is not None:
        # ⚠️ 2026-09-13 desert 整段黑畫面：pipeline 的相機掛在 camrig 底下，tilt 那段用
        #   cam.matrix_parent_inverse = cam_matrix.inverted() 會把相機的「local」basis 當成 world ⇒ 相機被搬走。
        #   (我的測試相機沒有 parent，所以沒測出來。) 修好之前：有 parent 就不做 tilt，只做平移。
        print(f"[animation] ⚠️ 相機有 parent({old_parent.name})，tilt 會把相機搬走 ⇒ 略過 tilt，只做平移",
              flush=True)
        tilt = 0.0
    cam.parent = rig
    if tilt:
        # 要「原地轉」就得讓 rig 坐在相機身上、朝向也一樣，否則會繞世界原點甩一大圈。
        # ⚠️ 順序：★先 parent 再設反矩陣★ —— 指派 .parent 會把 matrix_parent_inverse 重設成
        #    單位矩陣，先設就被吃掉(實測相機被搬到兩倍座標)。
        rig.matrix_world = cam_matrix
        cam.matrix_parent_inverse = cam_matrix.inverted()
    base_location = rig.location.copy()
    base_rotation = rig.rotation_euler.copy()
    rig.keyframe_insert("location", frame=scene.frame_start)
    if tilt:
        rig.keyframe_insert("rotation_euler", frame=scene.frame_start)
    rig.location = base_location + delta
    rig.keyframe_insert("location", frame=scene.frame_end)
    if tilt:
        # rig 跟相機同朝向 ⇒ 繞自己的 X 軸轉就是俯仰
        rig.rotation_euler = (base_rotation.x + tilt, base_rotation.y, base_rotation.z)
        rig.keyframe_insert("rotation_euler", frame=scene.frame_end)
    for fc in rig.animation_data.action.fcurves:
        for kp in fc.keyframe_points:
            kp.interpolation = "LINEAR"
    if abs(zoom - 1.0) > 1e-6 and getattr(cam, "data", None) is not None:
        lens0 = float(cam.data.lens)
        cam.data.lens = lens0
        cam.data.keyframe_insert("lens", frame=scene.frame_start)
        cam.data.lens = lens0 * zoom
        cam.data.keyframe_insert("lens", frame=scene.frame_end)
        if cam.data.animation_data and cam.data.animation_data.action:
            for fc in cam.data.animation_data.action.fcurves:
                if fc.data_path == "lens":
                    for kp in fc.keyframe_points:
                        kp.interpolation = "LINEAR"
        print(f"[animation] zoom 焦距 {lens0:.1f}mm → {lens0 * zoom:.1f}mm", flush=True)
    return (f"{direction} {delta.length:.2f}m"
            f"{f' + zoom ×{zoom:.2f}' if abs(zoom - 1.0) > 1e-6 else ''}"
            f"{f' + tilt {math.degrees(tilt):+.1f}°' if tilt else ''} (rig)")


def main() -> int:
    args = parse_args()
    if args.frame_start < 1 or args.frame_end < args.frame_start:
        raise SystemExit("invalid animation frame range")
    width, height = (int(value) for value in args.res.lower().split("x", 1))
    args.out_dir.mkdir(parents=True, exist_ok=True)

    bpy.ops.wm.open_mainfile(filepath=str(args.blend.resolve()))
    # Rigged assets keep their armature only in the render-hidden master
    # collection, which leaves the body in rest pose for the whole render.
    # This only changes armature visibility -- armatures draw no pixels -- so it
    # does not touch the motion state the final-scene contract validated.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from animation_visibility import repair_armature_visibility
    repair_armature_visibility()
    scene = bpy.context.scene
    if scene.camera is None:
        raise SystemExit("validated blend has no active camera")

    # Contract-critical rule: do not touch camera animation, constraints,
    # particle settings, instance collections, or point caches here.
    scene.frame_start = args.frame_start
    scene.frame_end = args.frame_end
    scene.render.fps = args.fps
    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.resolution_percentage = 100
    scene.render.engine = "CYCLES"
    scene.cycles.samples = args.samples
    scene.cycles.use_denoising = True
    scene.view_settings.view_transform = args.view
    scene.view_settings.exposure = args.exp
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.filepath = str(args.out_dir / "frame_")

    import render_device
    render_device.configure(scene, label="animation")
    move = apply_camera_move(
        scene, _pick_camera_move(args.camera_move, args.scene, str(args.blend)), args.camera_move_m)
    print(f"[animation] camera move: {move}", flush=True)

    print(
        f"[animation] exact scene motion, frames {args.frame_start}..{args.frame_end}, "
        f"camera={scene.camera.name}",
        flush=True,
    )
    bpy.ops.render.render(animation=True)
    print(f"[animation] frames -> {args.out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
