"""360 turntable renderer for a REAL Infinigen factory (Blender/bpy env).

Spawns the factory (like render_factory.py), then orbits a 50mm camera 360 degrees at
TWO fixed elevations — LEVEL (eye-level / 平視) and 3/4 TOP-DOWN (俯視) — writing
frames_dir/level_XXXX.png and frames_dir/top_XXXX.png. The caller assembles them into
looping GIFs. Reuses render_factory.py's spawn + fixed floor/lighting so the turntable
matches the still renders' look (same background, lighting, camera framing).

Usage (bpy env, e.g. the `blender` conda python):
    python render_factory_turntable.py <infinigen_root> <factory_fqn> <frames_dir> <seed> [n_frames]
"""
import math
import os
import sys
import traceback

import bpy
import mathutils

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render_factory as rf  # noqa: E402

# 3/4 top-down orbit ratios matching render_factory.force_camera's framing
# (offset (0.8,-1,0.7) * 1.4*size => horizontal ~1.79s, height ~0.98s).
TOP_RADIUS_RATIO = 1.79
TOP_HEIGHT_RATIO = 0.98
LEVEL_RADIUS_RATIO = 1.9


def place_orbit_camera(angle_rad, center, size, height_ratio, radius_ratio):
    for obj in [o for o in bpy.data.objects if o.type == "CAMERA"]:
        bpy.data.objects.remove(obj, do_unlink=True)
    center = mathutils.Vector(center)
    s = max(size, 0.5)
    r, h = s * radius_ratio, s * height_ratio
    cam_data = bpy.data.cameras.new("TurntableCam")
    cam_data.lens = 50
    cam = bpy.data.objects.new("TurntableCam", cam_data)
    bpy.context.collection.objects.link(cam)
    cam.location = center + mathutils.Vector((r * math.sin(angle_rad), -r * math.cos(angle_rad), h))
    cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
    bpy.context.scene.camera = cam


def main():
    root, fqn, frames_dir, seed = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
    n_frames = int(sys.argv[5]) if len(sys.argv) > 5 else 36
    if root not in sys.path:
        sys.path.insert(0, root)
    os.makedirs(frames_dir, exist_ok=True)

    rf.reset_scene()
    try:
        rf.spawn_factory(fqn, seed)
    except Exception:
        print("FACTORY_SPAWN_ERROR:\n" + traceback.format_exc())
    rf.force_floor()
    rf.force_lighting()
    center, size = rf.scene_bounds()

    scene = bpy.context.scene
    scene.render.resolution_x = 1024
    scene.render.resolution_y = 1024
    scene.render.resolution_percentage = 100
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.exposure = float(os.environ.get("TURNTABLE_EXPOSURE", "-1.5"))
    scene.cycles.samples = min(getattr(scene.cycles, "samples", 32) or 32, 32)
    scene.render.image_settings.file_format = "PNG"

    def _render_to(path):
        scene.render.filepath = path
        try:
            bpy.ops.render.render(write_still=True)
        except RuntimeError as e:                          # GPU OOM on shared boxes -> CPU
            print(f"GPU render failed ({e}); retrying on CPU...")
            scene.cycles.device = "CPU"
            bpy.ops.render.render(write_still=True)

    views = os.environ.get("TURNTABLE_VIEWS", "both")
    if views not in {"level", "both"}:
        raise ValueError(f"unsupported TURNTABLE_VIEWS={views!r}")

    for i in range(n_frames):
        angle = 2.0 * math.pi * i / n_frames
        place_orbit_camera(angle, center, size, 0.0, LEVEL_RADIUS_RATIO)
        _render_to(os.path.join(frames_dir, f"level_{i:04d}.png"))
        if views == "both":
            place_orbit_camera(angle, center, size, TOP_HEIGHT_RATIO, TOP_RADIUS_RATIO)
            _render_to(os.path.join(frames_dir, f"top_{i:04d}.png"))
        print(f"TURNTABLE_FRAME_OK: {i + 1}/{n_frames}")

    print("TURNTABLE_OK:", frames_dir)


if __name__ == "__main__":
    main()
