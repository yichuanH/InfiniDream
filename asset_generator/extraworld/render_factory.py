"""Render a REAL Infinigen factory's output to a PNG (Blender/bpy env).

Unlike the old plain-bpy render_object.py, this imports the actual Infinigen
factory class and spawns it the standard way (fac = Cls(seed); fac.spawn_asset).
The studio rig (floor / camera / lighting / render settings) is reused verbatim
from render_object.py.

Usage (must run in a python with `bpy` importable, e.g. the `blender` conda env):
    python render_factory.py <infinigen_root> <factory_fqn> <out_png> <seed> [exposure]

  infinigen_root : dir containing the importable `infinigen` package
  factory_fqn    : e.g. infinigen.assets.objects.fruits.apple.FruitFactoryApple
"""
import importlib
import sys
import traceback
from pathlib import Path

import bpy
import mathutils


def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        prefs.compute_device_type = "CUDA"
        prefs.get_devices()
        for d in prefs.devices:
            d.use = True
        scene.cycles.device = "GPU"
    except Exception:
        scene.cycles.device = "CPU"
    scene.cycles.samples = 48
    scene.render.resolution_x = 1024
    scene.render.resolution_y = 1024
    scene.render.film_transparent = False


def spawn_factory(factory_fqn: str, seed: int):
    module_path, cls_name = factory_fqn.rsplit(".", 1)
    mod = importlib.import_module(module_path)
    factory_cls = getattr(mod, cls_name)
    fac = factory_cls(seed)
    asset = fac.spawn_asset(seed)
    fac.finalize_assets(asset)
    _drop_to_floor(asset)
    return asset


def _drop_to_floor(asset):
    """Some factories spawn the asset hanging below z=0 (e.g. a fruit dangling
    from a stem at the origin). The fixed rig assumes the object rests ON the
    floor, so lift the whole asset until its lowest point sits at z=0."""
    min_z = 1e9
    for obj in ([asset] + list(asset.children_recursive)):
        if obj.type != "MESH":
            continue
        for corner in obj.bound_box:
            min_z = min(min_z, (obj.matrix_world @ mathutils.Vector(corner)).z)
    if min_z < 1e8:
        asset.location.z -= min_z
        bpy.context.view_layer.update()


def scene_bounds():
    mins = [1e9] * 3
    maxs = [-1e9] * 3
    found = False
    for obj in bpy.data.objects:
        if obj.type != "MESH" or obj.name.startswith("Fixed"):
            continue
        found = True
        for corner in obj.bound_box:
            w = obj.matrix_world @ mathutils.Vector(corner)
            for i in range(3):
                mins[i] = min(mins[i], w[i])
                maxs[i] = max(maxs[i], w[i])
    if not found:
        return mathutils.Vector((0, 0, 0)), 2.0
    center = mathutils.Vector([(mins[i] + maxs[i]) / 2 for i in range(3)])
    size = max(maxs[i] - mins[i] for i in range(3))
    return center, max(size, 0.5)


def force_floor():
    for obj in [o for o in bpy.data.objects if o.type == "MESH"]:
        bb = [obj.matrix_world @ mathutils.Vector(c) for c in obj.bound_box]
        xs = [v.x for v in bb]; ys = [v.y for v in bb]; zs = [v.z for v in bb]
        if max(max(xs) - min(xs), max(ys) - min(ys)) > 1.0 and (max(zs) - min(zs)) < 0.02:
            bpy.data.objects.remove(obj, do_unlink=True)
    bpy.ops.mesh.primitive_plane_add(size=500.0, location=(0, 0, 0))
    floor = bpy.context.active_object
    floor.name = "FixedFloor"
    mat = bpy.data.materials.new("FixedFloorMat")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (1, 1, 1, 1)
    bsdf.inputs["Roughness"].default_value = 0.9
    floor.data.materials.append(mat)


def force_camera():
    for obj in [o for o in bpy.data.objects if o.type == "CAMERA"]:
        bpy.data.objects.remove(obj, do_unlink=True)
    center, size = scene_bounds()
    dist = size * 1.4
    cam_data = bpy.data.cameras.new("FixedCam")
    cam_data.lens = 50
    cam = bpy.data.objects.new("FixedCam", cam_data)
    bpy.context.collection.objects.link(cam)
    cam.location = center + mathutils.Vector((dist * 0.8, -dist, dist * 0.7))
    cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
    bpy.context.scene.camera = cam


def force_lighting():
    for obj in [o for o in bpy.data.objects if o.type == "LIGHT"]:
        bpy.data.objects.remove(obj, do_unlink=True)
    center, size = scene_bounds()
    s = max(size, 0.5)
    world = bpy.data.worlds.new("FixedWorld")
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs[0].default_value = (1, 1, 1, 1)
        bg.inputs[1].default_value = 1.0
    bpy.context.scene.world = world

    def add_area(name, offset, energy, lsize):
        ld = bpy.data.lights.new(name, type="AREA")
        ld.energy = energy
        ld.size = lsize
        ob = bpy.data.objects.new(name, ld)
        ob.location = center + mathutils.Vector(offset)
        ob.rotation_euler = (center - ob.location).to_track_quat("-Z", "Y").to_euler()
        bpy.context.collection.objects.link(ob)

    add_area("FixedKey", (s * 1.6, -s * 1.8, s * 2.2), 350 * s * s, s * 2.0)
    add_area("FixedFill", (-s * 1.9, -s * 1.3, s * 1.4), 100 * s * s, s * 3.0)


def render(out_png: str, exposure=-1.5):
    scene = bpy.context.scene
    scene.render.resolution_x = 1024
    scene.render.resolution_y = 1024
    scene.render.resolution_percentage = 100
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.exposure = exposure
    scene.cycles.samples = min(getattr(scene.cycles, "samples", 48) or 48, 48)
    scene.render.filepath = out_png
    scene.render.image_settings.file_format = "PNG"
    try:
        bpy.ops.render.render(write_still=True)
    except RuntimeError as e:
        # Shared GPUs are often out of memory — fall back to CPU and retry.
        print(f"GPU render failed ({e}); retrying on CPU...")
        scene.cycles.device = "CPU"
        bpy.ops.render.render(write_still=True)


def main():
    infinigen_root, factory_fqn, out_png, seed = (
        sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]))
    exposure = float(sys.argv[5]) if len(sys.argv) > 5 else -1.5
    if infinigen_root not in sys.path:
        sys.path.insert(0, infinigen_root)
    Path(out_png).parent.mkdir(parents=True, exist_ok=True)
    reset_scene()
    try:
        spawn_factory(factory_fqn, seed)
    except Exception:
        print("FACTORY_SPAWN_ERROR:\n" + traceback.format_exc())
    _, size = scene_bounds()
    print(f"OBJECT_SIZE: {size:.4f}")
    force_floor()
    force_camera()
    force_lighting()
    render(out_png, exposure=exposure)
    print("OBJECT_OK", out_png)


if __name__ == "__main__":
    main()
