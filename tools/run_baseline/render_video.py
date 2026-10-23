"""Render a short video from an Infinigen scene.blend.

Camera: a simple linear dolly. We keyframe the EXISTING official camera from
its own starting pose, translating it along a chosen axis. We do not re-place
or re-aim it, so the framing stays the one Infinigen chose.

Scene motion: whatever the blend already contains. wind / turbulence /
leaf_particles / rain / creature idle rigs are all created by compose_nature
during the coarse task, so they are baked into the .blend and animate on their
own as the frame counter advances. Nothing here creates motion.

Usage:
  python render_video.py <scene.blend> <out_dir> [--frames 72] [--fps 24]
                         [--samples 64] [--dolly 6.0] [--res 1280x720]
                         [--axis right|forward|up]

Writes <out_dir>/frame_####.png, then mux them with ffmpeg.
"""

import argparse
import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector


# Camera presets, keyed by --preset.
#
# "land" is the tuned look for vegetated land biomes (forest, coast, mountain,
# canyon, cliff, plain...). Infinigen's own pose_cameras drops the camera down
# inside the canopy, so a plain ground-level dolly just ploughs through leaves.
# Lifting 8m and tilting only 12 degrees below the horizon clears the foliage
# while keeping trees as the subject -- verified on forest seed 0: foreground
# canopy, mid-ground valley and river, distant hills, leaves drifting past.
# Raising further (15m/30deg, 40m/40deg) turns it into a terrain map and loses
# the "inside a forest" feel.
#
# "underwater" leaves the camera where Infinigen put it: reef scenes are open
# enough that the default pose already frames well.
PRESETS = {
    # 2m up, 5 deg down: eye level among the trunks, horizon visible, canopy
    # overhead. Higher settings (8m/12deg, 15m/30deg, 40m/40deg) progressively
    # turn the shot into a terrain map and lose the "standing in a forest" feel.
    "land": dict(aerial=2.0, pitch=5.0, dolly=8.0),
    "underwater": dict(aerial=0.0, pitch=35.0, dolly=6.0),
}

# Framings offered by preview_angles.py. Keep these two tables in sync so that
# whichever angle looks best in the contact sheet can be rendered by name.
# Deliberately small nudges from Infinigen's own pose. Large moves rotate the
# coarse out-of-view terrain shell (0.07 marching cube/pixel vs 0.53 in view)
# into frame, where it shows as flat triangles.
ANGLES = {
    "ground": dict(aerial=0.0, pitch=0.0, back=0.0, side=0.0, yaw=0.0),
    "closeup": dict(aerial=0.5, pitch=4.0, back=-4.0, side=0.0, yaw=0.0),
    "wide": dict(aerial=1.5, pitch=6.0, back=8.0, side=0.0, yaw=-12.0),
    "eye": dict(aerial=4.0, pitch=8.0, back=3.0, side=6.0, yaw=10.0),
    "raised": dict(aerial=9.0, pitch=16.0, back=6.0, side=-6.0, yaw=-8.0),
}


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else sys.argv[1:]
    ap = argparse.ArgumentParser()
    ap.add_argument("blend")
    ap.add_argument("out_dir")
    ap.add_argument("--preset", choices=sorted(PRESETS),
                    help="camera preset; explicit --aerial/--pitch/--dolly still win")
    ap.add_argument("--frames", type=int, default=72)
    ap.add_argument("--fps", type=int, default=24)
    ap.add_argument("--samples", type=int, default=64)
    ap.add_argument("--dolly", type=float, default=6.0, help="metres travelled over the whole shot")
    ap.add_argument("--axis", default="right", choices=["right", "forward", "up"])
    ap.add_argument("--res", default="1280x720")
    # Aerial mode: lift the camera above the canopy and tilt it down. Infinigen
    # picks static camera poses that sit inside dense vegetation, so in forest
    # biomes a ground-level dolly just ploughs through leaves.
    ap.add_argument("--aerial", type=float, default=0.0,
                    help="metres to raise the camera before the dolly (0 = off)")
    ap.add_argument("--pitch", type=float, default=35.0,
                    help="downward tilt in degrees when --aerial is used")
    ap.add_argument("--fish-fraction", type=float, default=1.0,
                    help="scale particle counts (fish schools etc), 1.0 = unchanged")
    ap.add_argument("--motion", default="dolly",
                    choices=["dolly", "static", "orbit", "zoom", "pan"],
                    help="dolly=slide sideways, static=locked off, "
                         "orbit=arc around a pivot, zoom=push in/out, "
                         "pan=rotate in place (indoor: any translation hits a wall)")
    ap.add_argument("--pan-degrees", type=float, default=16.0,
                    help="pan 的總掃掠角度(以起始朝向為中心，左右各一半)")
    ap.add_argument("--orbit-radius", type=float, default=25.0,
                    help="orbit only: metres ahead of camera to circle around")
    ap.add_argument("--orbit-degrees", type=float, default=40.0,
                    help="orbit only: total arc swept over the shot")
    ap.add_argument("--angle", choices=sorted(ANGLES),
                    help="camera framing from the preview_angles presets")
    args = ap.parse_args(argv)

    # Apply the preset only for values the caller did not pass explicitly, so
    # "--preset land --dolly 20" keeps the preset's framing but overrides reach.
    explicit = {a.lstrip("-").replace("-", "_").split("=")[0] for a in argv}
    args.back = 0.0
    args.side = 0.0
    args.yaw = 0.0
    # --angle wins over --preset: it names a framing you picked from a contact
    # sheet, which is more specific than a per-biome default.
    if args.angle:
        for key, value in ANGLES[args.angle].items():
            if key not in explicit:
                setattr(args, key, value)
        print(f"angle={args.angle} aerial={args.aerial} pitch={args.pitch} "
              f"back={args.back}")
    elif args.preset:
        for key, value in PRESETS[args.preset].items():
            if key not in explicit:
                setattr(args, key, value)
        print(f"preset={args.preset} aerial={args.aerial} pitch={args.pitch} dolly={args.dolly}")
    return args


def pick_camera(scene):
    if scene.camera is not None:
        return scene.camera
    cams = [o for o in scene.objects if o.type == "CAMERA"]
    if not cams:
        raise SystemExit("no camera in blend")
    scene.camera = cams[0]
    return cams[0]


def main():
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    bpy.ops.wm.open_mainfile(filepath=args.blend)
    scene = bpy.context.scene

    cam = pick_camera(scene)

    # Fish schools are BOIDS particle systems, and three separate things must be
    # undone before they survive past frame 1:
    #
    #  1. fish.py sets lifetime = scene.frame_end - scene.frame_start + 1, which
    #     is evaluated during the coarse task while the scene is still a single
    #     frame -- so lifetime comes out as 1 and every fish dies after frame 1.
    #  2. The point cache is BAKED with only that one frame in it. While
    #     is_baked is True Blender replays the bake and ignores any settings we
    #     change here, so the bake must be freed first.
    #  3. The cache frame range must cover the whole shot so the boids
    #     simulation re-runs across it.
    #
    # Freeing the bake also means the flock actually swims: boids is a real
    # simulation, so the fish move rather than just sitting in place.
    # Blender does not simulate particles for objects hidden in the VIEWPORT,
    # even though they still render. Infinigen ships the falling-leaf emitter
    # (and its "particles" collection) with hide_viewport=True, so the leaves
    # never accumulate and only frame 1 -- served straight from the baked cache
    # -- shows any. Unhide before simulating; render visibility is unaffected.
    # Only the emitters themselves, not every hidden object: Infinigen also
    # hides proxy//placeholder vegetation in the viewport, and unhiding that
    # drops extra geometry right in front of the lens.
    for obj in bpy.data.objects:
        if not getattr(obj, "particle_systems", None):
            continue
        obj.hide_viewport = False
        for coll in obj.users_collection:
            coll.hide_viewport = False

    psys_objs = [o for o in bpy.data.objects if getattr(o, "particle_systems", None)]
    # Only free the bake for BOIDS systems (fish schools), where the 1-frame
    # bake is what pins the flock in place and freeing it makes them swim.
    # Do NOT free NEWTON weather emitters (falling leaves): their leaves live
    # only in that bake, so freeing it removes the leaves that frame 1 does
    # have and gives nothing back -- the emitter's window has already closed.
    if any(
        ps.settings.physics_type == "BOIDS"
        for o in psys_objs
        for ps in o.particle_systems
    ):
        bpy.ops.ptcache.free_bake_all()
    for obj in psys_objs:
        for psys in obj.particle_systems:
            st = psys.settings
            if st.type != "EMITTER":
                continue
            # Two distinct symptoms, both fixed the same way:
            #   fish schools get lifetime=1 (dies after frame 1)
            #   weather emitters (falling leaves) keep a long lifetime but
            #     frame_end=1, so nothing is emitted after the first frame.
            # Either one leaves the scene empty from frame 2 onward.
            if st.lifetime >= args.frames and st.frame_end >= args.frames:
                continue
            st.lifetime = max(st.lifetime, args.frames * 10)
            # The emission WINDOW matters as much as the lifetime. Weather
            # emitters (falling leaves) are a plane 5m above the camera rig,
            # following it via COPY_LOCATION, and ship frame_start=-1023,
            # frame_end=1 -- everything is emitted before the shot starts and
            # has fallen past the camera by frame 2. Keep a negative start so
            # the air is already full on frame 1, push the end past our last
            # frame so new leaves keep spawning, and shorten the lifetime so
            # they are recycled instead of falling away for good.
            if st.frame_end < args.frames:
                st.frame_start = -float(args.frames)
                st.frame_end = float(args.frames)
                st.lifetime = float(args.frames)
            cache = psys.point_cache
            cache.frame_start = 1
            cache.frame_end = args.frames
            msg = f"particles: {obj.name[:38]} lifetime -> {st.lifetime}"
            # Per-school population comes from boid_swarm.py's
            # count = volume / (avg_size**3 * child_vol); `volume` is passed
            # positionally so gin cannot reach it. Scaling st.count here is the
            # only way to thin a school without re-running the coarse task.
            if args.fish_fraction != 1.0:
                st.count = max(1, int(st.count * args.fish_fraction))
                msg += f", count -> {st.count}"
            print(msg + f", cache 1-{args.frames}")

    # Infinigen parents cameras to a camera_rig with constraints, so cam.location
    # is not the real world position. Drive the top-level parent when there is
    # one, and read the true pose off the evaluated matrix.
    target = cam
    while target.parent is not None:
        target = target.parent

    deps = bpy.context.evaluated_depsgraph_get()
    mw = cam.evaluated_get(deps).matrix_world
    cam_right = Vector((mw[0][0], mw[1][0], mw[2][0])).normalized()
    cam_fwd = -Vector((mw[0][2], mw[1][2], mw[2][2])).normalized()
    cam_up = Vector((mw[0][1], mw[1][1], mw[2][1])).normalized()
    direction = {"right": cam_right, "forward": cam_fwd, "up": cam_up}[args.axis]

    print(f"camera={cam.name} rig={target.name}")
    print(f"true cam pos={tuple(round(v, 2) for v in mw.translation)} dolly axis={args.axis}")

    target.animation_data_clear()

    # Aerial mode: raise the rig above the canopy and pitch the camera down, so
    # the shot looks over the treetops instead of through them. The camera's
    # own rotation is driven by rig constraints, so clear those and aim the
    # camera object directly.
    if args.aerial > 0 or args.back or args.side or args.yaw:
        for c in list(cam.constraints):
            cam.constraints.remove(c)
        for c in list(target.constraints):
            target.constraints.remove(c)
        target.location.z += args.aerial
        # Blender cameras look down -Z, so rotation_euler.x = 0 points straight
        # down and 90 deg is the horizon. Tilting `pitch` degrees below the
        # horizon therefore means 90 - pitch, applied to the camera's WORLD
        # rotation (the rig may carry its own orientation, so set it there).
        cam.rotation_mode = "XYZ"
        cam.rotation_euler = (
            math.radians(90.0 - args.pitch),
            0.0,
            cam.rotation_euler.z + math.radians(args.yaw),
        )
        target.rotation_mode = "XYZ"
        target.rotation_euler = (0.0, 0.0, target.rotation_euler.z)
        bpy.context.view_layer.update()
        # Move along the re-aimed basis (back negative = push in).
        if args.back or args.side:
            deps2 = bpy.context.evaluated_depsgraph_get()
            m2 = cam.evaluated_get(deps2).matrix_world
            fwd2 = -Vector((m2[0][2], m2[1][2], m2[2][2])).normalized()
            right2 = Vector((m2[0][0], m2[1][0], m2[2][0])).normalized()
            target.location -= fwd2 * args.back
            target.location += right2 * args.side
            bpy.context.view_layer.update()
        print(f"aerial: +{args.aerial}m, pitch {args.pitch} deg, "
              f"back {args.back}m, side {args.side}m, yaw {args.yaw} deg")

    # Re-read the camera basis after any repositioning so motions use the
    # final orientation, not the pose the blend shipped with.
    deps = bpy.context.evaluated_depsgraph_get()
    mw = cam.evaluated_get(deps).matrix_world
    cam_right = Vector((mw[0][0], mw[1][0], mw[2][0])).normalized()
    cam_fwd = -Vector((mw[0][2], mw[1][2], mw[2][2])).normalized()
    cam_up = Vector((mw[0][1], mw[1][1], mw[2][1])).normalized()
    direction = {"right": cam_right, "forward": cam_fwd, "up": cam_up}[args.axis]

    scene.frame_start = 1
    scene.frame_end = args.frames
    start = target.location.copy()

    # The camera's own rotation must be keyframed too. Infinigen ships animated
    # camera rotation in the blend, so without keys of our own the first
    # frame_set() restores the original pose and undoes the aerial tilt --
    # which drops the camera straight back inside the canopy.
    if args.aerial > 0:
        cam.animation_data_clear()
        for f in (1, args.frames):
            cam.keyframe_insert(data_path="rotation_euler", frame=f)

    if args.motion == "static":
        # Camera locked off; only the scene itself moves (leaves, wind, fish).
        target.keyframe_insert(data_path="location", frame=1)
        target.keyframe_insert(data_path="location", frame=args.frames)

    elif args.motion == "dolly":
        # Straight slide along one camera axis.
        target.location = start
        target.keyframe_insert(data_path="location", frame=1)
        target.location = start + direction * args.dolly
        target.keyframe_insert(data_path="location", frame=args.frames)

    elif args.motion == "zoom":
        # Push in (or pull out) along the view direction.
        target.location = start
        target.keyframe_insert(data_path="location", frame=1)
        target.location = start + cam_fwd * args.dolly
        target.keyframe_insert(data_path="location", frame=args.frames)

    elif args.motion == "pan":
        # ★原地轉★(2026-08-06 為室內加) —— 位置完全不動，只掃 yaw。
        #   室內任何平移都可能穿牆：實測 Bedroom 只推 1.5m，畫面右側就被牆掃進來。
        #   orbit 也不行 —— 它是繞前方樞紐畫弧，相機仍然位移 orbit_radius。
        #   ⚠️ 必須先 animation_data_clear()：Infinigen 的 blend ★自帶相機旋轉動畫★，
        #      不清掉的話第一次 frame_set() 會把我們的 keyframe 蓋回原本的姿態。
        cam.animation_data_clear()
        cam.rotation_mode = "XYZ"
        base_yaw = cam.rotation_euler.z
        sweep = math.radians(args.pan_degrees)
        target.keyframe_insert(data_path="location", frame=1)
        target.keyframe_insert(data_path="location", frame=args.frames)
        for i in range(args.frames):
            t = i / max(1, args.frames - 1)
            cam.rotation_euler = (cam.rotation_euler.x, cam.rotation_euler.y,
                                  base_yaw - sweep / 2 + sweep * t)
            cam.keyframe_insert(data_path="rotation_euler", frame=i + 1)
        print(f"pan: {args.pan_degrees} deg 原地掃掠，位置不動")

    elif args.motion == "orbit":
        # Arc around a pivot `orbit_radius` metres ahead of the camera, keeping
        # it centred. Both location and rotation are keyed each step, so the
        # camera swings around the subject instead of sliding past it.
        pivot = mw.translation + cam_fwd * args.orbit_radius
        offset = target.location - Vector(
            (pivot.x, pivot.y, target.location.z)
        )
        base_yaw = cam.rotation_euler.z
        sweep = math.radians(args.orbit_degrees)
        cam.rotation_mode = "XYZ"
        for i in range(args.frames):
            t = i / max(1, args.frames - 1)
            a = sweep * t
            ca, sa = math.cos(a), math.sin(a)
            target.location = Vector((
                pivot.x + offset.x * ca - offset.y * sa,
                pivot.y + offset.x * sa + offset.y * ca,
                start.z,
            ))
            target.keyframe_insert(data_path="location", frame=i + 1)
            cam.rotation_euler = (cam.rotation_euler.x, cam.rotation_euler.y,
                                  base_yaw + a)
            cam.keyframe_insert(data_path="rotation_euler", frame=i + 1)
        print(f"orbit: {args.orbit_degrees} deg around pivot "
              f"{args.orbit_radius}m ahead")

    # Ease in/out so the move does not start and stop abruptly.
    if target.animation_data and target.animation_data.action:
        for fc in target.animation_data.action.fcurves:
            for kp in fc.keyframe_points:
                kp.interpolation = "BEZIER"
                kp.easing = "EASE_IN_OUT"

    # Render settings
    w, h = (int(x) for x in args.res.lower().split("x"))
    scene.render.resolution_x = w
    scene.render.resolution_y = h
    scene.render.resolution_percentage = 100
    scene.render.fps = args.fps
    scene.render.engine = "CYCLES"
    scene.cycles.samples = args.samples
    scene.cycles.use_denoising = True
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"

    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        prefs.compute_device_type = "OPTIX"
        prefs.get_devices()
        for d in prefs.devices:
            d.use = d.type != "CPU"
        scene.cycles.device = "GPU"
    except Exception as e:  # noqa: BLE001
        print("GPU setup warning:", e)

    # Render the whole range in one go, exactly as the official pipeline does.
    # Rendering frame-by-frame with frame_set() + write_still breaks NEWTON
    # particle systems: the falling-leaf emitter only accumulates correctly
    # when Blender steps the simulation itself across a continuous render, so
    # per-frame rendering yields leaves on frame 1 and an empty sky after.
    scene.render.filepath = str(out_dir / "frame_")
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    bpy.ops.render.render(animation=True)
    print(f"RENDERED {args.frames} frames", flush=True)

    print("RENDER_DONE", out_dir)


if __name__ == "__main__":
    main()
