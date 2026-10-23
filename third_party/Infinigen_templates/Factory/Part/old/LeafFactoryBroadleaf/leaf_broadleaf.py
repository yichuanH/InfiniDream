"""
================================================================================
LeafFactoryBroadleaf  —  a broad flat LEAF   [PART / ELEMENT]
  readable plain-bpy port of  <INF>/leaves/leaf_broadleaf.py  (LeafFactoryBroadleaf)
================================================================================

WHAT THIS IS
  ONE broadleaf blade: wide ovate, gently serrated edge, a central midrib crease,
  stem at the origin, tip toward +Y, lying flat (normal +Z). A broadleaf tree
  [ENTRY] (oak/beech/generic deciduous) instances MANY of these.

WHY A TEMPLATE (not the source)
  Source ~1300 lines of Geometry-Nodes growing the blade from a vein contour. The
  SILHOUETTE that matters: a broad ovate outline with fine marginal teeth and a
  midrib fold. Reproduced here parametrically in plain bpy.

SPECIES OUTLINE
  width      0.6   half-width of the widest part (bigger = rounder leaf)
  widest_at  0.45  where along the length the leaf is widest (0 stem .. 1 tip)
  teeth      True  fine serrations along the margin
  fold       0.08  midrib crease depth (raises the centre line out of plane)
  scale      0.4   leaf size in metres

COLOR  green (summer) / warm (autumn)

HARNESS RULES (fixed studio owns cameras/lights/floor/world/render)
  - build(seed=0) creates the leaf at the origin; returns nothing.
  - Only bpy / math / numpy / mathutils / random. Real mesh; no textures.
================================================================================
"""

import bpy
import numpy as np


def _blade_material(color):
    mat = bpy.data.materials.new("broadleaf")
    mat.use_nodes = True
    b = mat.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*color, 1.0)
    b.inputs["Roughness"].default_value = 0.6
    if "Subsurface Weight" in b.inputs:
        b.inputs["Subsurface Weight"].default_value = 0.1
    return mat


def _face_up(obj):
    if obj.data.polygons and obj.data.polygons[0].normal.z < 0:
        obj.data.flip_normals()
        obj.data.update()


def build(seed=0, scale=0.4, season="summer", width=0.6, widest_at=0.4,
          teeth=True, fold=0.08, n=20):
    rng = np.random.RandomState(seed)
    color = (0.55, 0.30, 0.06) if season == "autumn" else (0.15, 0.40, 0.12)

    # ovate width profile along the length: skewed bump peaking near widest_at,
    # tapering to a point at both the stem (0) and the tip (1).
    t = np.linspace(0, 1, n)
    k = np.log(0.5) / np.log(widest_at)                  # skew so the peak sits at widest_at
    w = np.sin(np.pi * t ** k) * width
    if teeth:
        w = w * (1 + 0.05 * np.sin(t * (n - 1) * np.pi))  # fine marginal serration

    # CCW boundary: up the right (+x) margin to the tip, down the left (-x) margin.
    boundary = [(w[i], t[i]) for i in range(n)] + [(-w[n - 1 - i], t[n - 1 - i]) for i in range(n)]
    # triangle-fan from the leaf centroid for a clean fill; raise a midrib ridge.
    cy = float(np.mean(t))
    verts = [(0.0, cy, fold)] + [(x, y, fold * np.sin(np.pi * y) if abs(x) < 1e-6 else 0.0)
                                 for (x, y) in boundary]
    m = len(boundary)
    faces = [(0, i, i + 1) for i in range(1, m)] + [(0, m, 1)]
    me = bpy.data.meshes.new("broadleaf")
    me.from_pydata(verts, [], faces)
    me.update()
    obj = bpy.data.objects.new("broadleaf", me)
    bpy.context.scene.collection.objects.link(obj)
    obj.data.materials.append(_blade_material(color))
    _face_up(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    obj.rotation_euler.x = np.pi / 2           # stand the blade up (face toward -Y camera)
    obj.scale = (scale, scale, scale)
    bpy.ops.object.transform_apply(rotation=True, scale=True)
    bpy.ops.object.shade_smooth()
    obj.name = "broadleaf"
    return obj


if __name__ == "__main__":
    build(0)
