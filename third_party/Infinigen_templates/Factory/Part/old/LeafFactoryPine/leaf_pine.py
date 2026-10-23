"""
================================================================================
LeafFactoryPine  —  a PINE NEEDLE cluster (fascicle)   [PART / ELEMENT]
  readable plain-bpy port of  <INF>/leaves/leaf_pine.py  (LeafFactoryPine)
  (see also <INF>/particles/pine_needle.py PineNeedleFactory for a single needle)
================================================================================

WHAT THIS IS
  ONE fascicle: a small bundle of long thin needles joined at a base sheath,
  fanning out and up, base at the origin. A conifer/pine tree [ENTRY] instances
  MANY of these along its twigs instead of broad leaves.

WHY A TEMPLATE (not the source)
  Source builds needles from a swept profile in Geometry Nodes. What matters: a
  cluster of N straight, slightly-splayed thin needles of length ~needle_len.
  Reproduced with thin tapered curves in plain bpy.

SPECIES OUTLINE
  n_needle    5     needles per fascicle (pine: 2,3, or 5 depending on species)
  needle_len  0.9   needle length in metres (relative; a pine needle is long/thin)
  spread      18deg half-angle the needles fan out from the fascicle axis
  radius      0.006 needle thickness

COLOR  dark blue-green (evergreen)

HARNESS RULES (fixed studio owns cameras/lights/floor/world/render)
  - build(seed=0) creates the fascicle at the origin; returns nothing.
  - Only bpy / math / numpy / mathutils / random. Real joined mesh; no particles.
================================================================================
"""

import bpy
import numpy as np
from mathutils import Vector, Matrix


def _needle_material():
    mat = bpy.data.materials.new("pine_needle")
    mat.use_nodes = True
    b = mat.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (0.09, 0.24, 0.12, 1.0)
    b.inputs["Roughness"].default_value = 0.55
    return mat


def _needle(length, radius, direction):
    """A single tapered needle as a POLY curve -> mesh along `direction`."""
    cu = bpy.data.curves.new("needle", 'CURVE')
    cu.dimensions = '3D'
    cu.bevel_depth = 1.0
    cu.resolution_u = 1
    sp = cu.splines.new('POLY')
    steps = 6
    sp.points.add(steps - 1)
    for i in range(steps):
        t = i / (steps - 1)
        p = direction * (t * length)
        sp.points[i].co = (p.x, p.y, p.z, 1.0)
        sp.points[i].radius = radius * (1 - 0.9 * t)   # taper to a fine tip
    obj = bpy.data.objects.new("needle", cu)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def build(seed=0, n_needle=5, needle_len=0.9, spread=18.0, radius=0.006):
    rng = np.random.RandomState(seed)
    mat = _needle_material()
    axis = Vector((0, 0.15, 1)).normalized()           # fascicle points up and +Y
    sp = np.deg2rad(spread)
    needles = []
    for k in range(n_needle):
        az = 2 * np.pi * k / n_needle
        # tilt the axis outward by `spread` around a per-needle azimuth
        perp = axis.cross(Vector((1, 0, 0)))
        if perp.length < 1e-4:
            perp = axis.cross(Vector((0, 1, 0)))
        perp.normalize()
        perp = Matrix.Rotation(az, 3, axis) @ perp
        d = (Matrix.Rotation(sp * rng.uniform(0.7, 1.2), 3, perp) @ axis).normalized()
        nd = _needle(needle_len * rng.uniform(0.85, 1.05), radius, d)
        nd.data.materials.append(mat)
        needles.append(nd)

    bpy.ops.object.select_all(action='DESELECT')
    for o in needles:
        o.select_set(True)
    bpy.context.view_layer.objects.active = needles[0]
    bpy.ops.object.convert(target='MESH')
    bpy.ops.object.join()
    obj = bpy.context.active_object
    obj.name = "pine_needle"
    bpy.ops.object.shade_smooth()
    return obj


if __name__ == "__main__":
    build(0)
