"""
================================================================================
BranchFactory  —  a single leafy TWIG   [PART / ELEMENT]
  readable plain-bpy port of  <INF>/trees/branch.py  (generate_branch)
================================================================================

WHAT THIS IS
  ONE short twig: a slightly curved tapered stem with small leaves spaced along it,
  base at the origin, growing along +Y. A GenericTreeFactory / bush instances MANY
  of these on its outer boughs to make foliage. Never pick this as a whole object.

WHY A TEMPLATE (not the source)
  The source builds the twig with Geometry-Nodes (CurveLine -> ResampleCurve ->
  points -> instance leaf collection along the curve, with a surface-bump stem).
  This template keeps that exact recipe — resample a curve into N points, drop a
  leaf at each with alternating rotation — in clear plain bpy.

STRUCTURE (matches generate_branch kwargs)
  resolution     ~12   points the stem curve is resampled to (leaf sites)
  overall_radius 0.02  stem thickness at the base (tapers to a tip)
  length         ~0.6  twig length in metres
  leaf_density   ~15   leaves per unit length -> n_leaf = density * length
  leaf_scale     0.3   size of each instanced leaf
  leaf_rot       45deg alternating twist so leaves fan left/right up the twig

FOLIAGE ELEMENT
  Each site instances ONE leaf. By default a simple ovate blade (same silhouette as
  Part/LeafFactory/leaf.py). Swap in any leaf [PART] (maple/broadleaf/ginko/pine)
  to change the tree species — that is the ONE choice that makes a maple vs a cherry.

HARNESS RULES (fixed studio owns cameras/lights/floor/world/render)
  - build(seed=0) creates the twig at the origin; returns nothing.
  - Only bpy / math / numpy / mathutils / random. Real joined mesh; no particles.
================================================================================
"""

import bpy
import numpy as np
from mathutils import Vector, Matrix


def _stem_material():
    mat = bpy.data.materials.new("twig")
    mat.use_nodes = True
    b = mat.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (0.25, 0.16, 0.09, 1.0)
    b.inputs["Roughness"].default_value = 0.9
    return mat


def _leaf_material():
    mat = bpy.data.materials.new("leaf")
    mat.use_nodes = True
    b = mat.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (0.16, 0.42, 0.13, 1.0)
    b.inputs["Roughness"].default_value = 0.6
    if "Subsurface Weight" in b.inputs:
        b.inputs["Subsurface Weight"].default_value = 0.1
    return mat


def _simple_leaf(size):
    """A small ovate blade (same silhouette family as Part/LeafFactory/leaf.py)."""
    n = 10
    a = np.linspace(0, np.pi, n)
    x = np.sin(a) * 0.5
    y = -np.cos(0.9 * (a - 0.3))
    verts = ([( xi * size,  (yi + 1) * size * 0.6, 0) for xi, yi in zip(x, y)] +
             [(-xi * size,  (yi + 1) * size * 0.6, 0) for xi, yi in zip(x[::-1], y[::-1])])
    face = list(range(len(verts)))
    me = bpy.data.meshes.new("leaf")
    me.from_pydata(verts, [], [face])
    me.update()
    return me


def _curved_stem_points(rng, length, resolution):
    """Resampled slightly-curved stem centre-line (CurveLine -> ResampleCurve)."""
    bend = rng.uniform(-0.15, 0.15)
    pts = []
    for i in range(resolution):
        t = i / (resolution - 1)
        pts.append(Vector((bend * length * t * t, t * length, 0.0)))
    return pts


def build(seed=0, length=0.6, resolution=12, overall_radius=0.02,
          leaf_density=15.0, leaf_scale=0.3, leaf_rot=45.0):
    rng = np.random.RandomState(seed)
    pts = _curved_stem_points(rng, length, resolution)

    # 1. stem: POLY curve with per-point radius taper -> mesh (robust, like source)
    cu = bpy.data.curves.new("twig", 'CURVE')
    cu.dimensions = '3D'
    cu.bevel_depth = 1.0
    cu.resolution_u = 2
    sp = cu.splines.new('POLY')
    sp.points.add(len(pts) - 1)
    for i, p in enumerate(pts):
        t = i / (len(pts) - 1)
        sp.points[i].co = (p.x, p.y, p.z, 1.0)
        sp.points[i].radius = overall_radius * (1 - 0.85 * t)
    stem = bpy.data.objects.new("twig", cu)
    bpy.context.scene.collection.objects.link(stem)
    stem.data.materials.append(_stem_material())
    bpy.context.view_layer.objects.active = stem
    stem.select_set(True)
    bpy.ops.object.convert(target='MESH')
    stem = bpy.context.active_object

    # 2. instance a leaf at each site up the twig, alternating twist (leaf_rot)
    leaf_mat = _leaf_material()
    n_leaf = max(3, int(leaf_density * length))
    leaves = []
    for k in range(n_leaf):
        t = (k + 1) / (n_leaf + 1)
        idx = min(len(pts) - 2, int(t * (len(pts) - 1)))
        site = pts[idx]
        tangent = (pts[idx + 1] - pts[idx]).normalized()
        me = _simple_leaf(leaf_scale * rng.uniform(0.8, 1.1))
        lf = bpy.data.objects.new("leaf", me)
        bpy.context.scene.collection.objects.link(lf)
        lf.data.materials.append(leaf_mat)
        # alternate left/right around the stem, tilt outward from the tangent
        twist = np.deg2rad(leaf_rot) * (1 if k % 2 == 0 else -1)
        elev = rng.uniform(0.3, 0.7)
        R = (Matrix.Rotation(twist, 4, 'Z')
             @ Matrix.Rotation(elev, 4, 'X'))
        lf.matrix_world = Matrix.Translation(site) @ R
        leaves.append(lf)

    # 3. join stem + leaves into one twig mesh
    bpy.ops.object.select_all(action='DESELECT')
    for o in leaves:
        o.select_set(True)
    stem.select_set(True)
    bpy.context.view_layer.objects.active = stem
    bpy.ops.object.join()
    twig = bpy.context.active_object
    twig.name = "branch"
    bpy.ops.object.shade_smooth()
    return twig


if __name__ == "__main__":
    build(0)
