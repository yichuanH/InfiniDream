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
import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector

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
    return ap.parse_args(argv)


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
    scene.frame_set(args.frame)

    wanted = [a.strip() for a in args.angles.split(",") if a.strip()]
    unknown = [a for a in wanted if a not in ANGLES]
    if unknown:
        raise SystemExit(f"unknown angle(s): {unknown}; choose from {list(ANGLES)}")

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

        scene.render.filepath = str(out_dir / f"angle_{name}")
        bpy.ops.render.render(write_still=True)
        print(
            f"ANGLE {name}: aerial={cfg['aerial']}m pitch={cfg['pitch']}deg "
            f"back={cfg['back']}m side={cfg['side']}m yaw={cfg['yaw']}deg cam=({pos.x:.1f},{pos.y:.1f},{pos.z:.1f}) "
            f"-- {cfg['note']}",
            flush=True,
        )

    print("PREVIEW_DONE", out_dir)


if __name__ == "__main__":
    main()
