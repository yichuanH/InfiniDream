"""
================================================================================
LeafFactoryV2  —  a detailed veined LEAF   [PART / ELEMENT]
  readable plain-bpy port of  <INF>/leaves/leaf_v2.py  (LeafFactoryV2)
================================================================================

WHAT THIS IS
  ONE ovate leaf with a raised midrib and a few lateral veins pressed into the
  blade (more detailed than the generic LeafFactory blade), stem at the origin,
  tip toward +Y, lying flat (normal +Z). A generic broadleaf tree [ENTRY] can
  instance THIS when it wants visible venation.

WHY A TEMPLATE (not the source)
  Source ~grows the blade + vein network in Geometry Nodes. The readable essence:
  an ovate outline (like LeafFactory) plus a midrib ridge and lateral vein ridges
  built as real raised geometry. Reproduced parametrically in plain bpy.

SPECIES OUTLINE / DETAIL
  leaf_width  0.5   half-width of the blade
  n_vein      5     lateral vein pairs pressed into the blade
  vein_raise  0.03  height of the midrib/vein ridges out of plane
  scale       0.3   leaf size in metres

COLOR  green (summer) / warm (autumn)

HARNESS RULES (fixed studio owns cameras/lights/floor/world/render)
  - build(seed=0) creates the leaf at the origin; returns nothing.
  - Only bpy / math / numpy / mathutils / random. Real mesh; no textures.
================================================================================
"""

import bpy
import numpy as np


def _blade_material(color):
    mat = bpy.data.materials.new("leaf_v2")
    mat.use_nodes = True
    b = mat.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*color, 1.0)
    b.inputs["Roughness"].default_value = 0.6
    if "Subsurface Weight" in b.inputs:
        b.inputs["Subsurface Weight"].default_value = 0.1
    return mat


def build(seed=0, scale=0.3, season="summer", leaf_width=0.5, vein_raise=0.03,
          nu=8, nv=16):
    """Build the blade as a UV grid (nu across x nv along y) so we can raise a
    midrib + lateral veins as real geometry, then trim to an ovate outline."""
    rng = np.random.RandomState(seed)
    color = (0.5, 0.28, 0.06) if season == "autumn" else (0.14, 0.42, 0.12)

    verts, faces = [], []
    for j in range(nv + 1):
        v = j / nv                                       # 0 stem -> 1 tip
        y = v
        half = np.sin(np.pi * v) * leaf_width            # ovate width profile
        half = max(half, 1e-4)
        row = []
        for i in range(nu + 1):
            u = i / nu - 0.5                              # -0.5..0.5 across width
            x = u * 2 * half
            # midrib ridge (centre) + lateral vein ripples raise z
            midrib = vein_raise * np.exp(-((u * 2) ** 2) / 0.05)
            lateral = vein_raise * 0.4 * np.cos(v * 5 * np.pi) * (1 - abs(u) * 2)
            z = midrib + max(lateral, 0) * (abs(u) > 0.05)
            row.append(len(verts))
            verts.append((x, y, z))
        if j > 0:
            prev = list(range(len(verts) - 2 * (nu + 1), len(verts) - (nu + 1)))
            cur = list(range(len(verts) - (nu + 1), len(verts)))
            for i in range(nu):
                faces.append((prev[i], prev[i + 1], cur[i + 1], cur[i]))

    me = bpy.data.meshes.new("leaf_v2")
    me.from_pydata(verts, [], faces)
    me.update()
    obj = bpy.data.objects.new("leaf_v2", me)
    bpy.context.scene.collection.objects.link(obj)
    obj.data.materials.append(_blade_material(color))
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    obj.rotation_euler.x = np.pi / 2           # stand the blade up (face toward -Y camera)
    obj.scale = (scale, scale, scale)
    bpy.ops.object.transform_apply(rotation=True, scale=True)
    bpy.ops.object.shade_smooth()
    obj.name = "leaf_v2"
    return obj


if __name__ == "__main__":
    build(0)
