"""
================================================================================
LeafFactoryMaple  —  a palmate MAPLE leaf   [PART / ELEMENT]
  readable plain-bpy port of  <INF>/leaves/leaf_maple.py  (LeafFactoryMaple)
================================================================================

WHAT THIS IS
  ONE maple leaf: flat, palmate (hand-shaped) with 5 pointed lobes and toothed
  edges, stem end at the origin, tip fan opening toward +Y. A maple tree [ENTRY]
  instances MANY of these. Choosing THIS element (vs the generic blade) is what
  makes a maple tree. Never pick it as a whole object.

WHY A TEMPLATE (not the source)
  The source is ~1350 lines of Geometry-Nodes DSL that grows the blade from a vein
  skeleton with a contour node graph. What matters for the SILHOUETTE is: 5 palmate
  lobes with sharp sinuses and serrated edges. This template reproduces that outline
  parametrically in clear plain bpy.

SPECIES OUTLINE (the maple signature)
  n_lobes    5     lobe tips fanned across the upper half-plane
  sinus      0.45  depth of the notches between lobes (fraction of lobe radius)
  teeth      True  small serrations along each lobe edge
  scale      0.5   leaf size in metres (source LeafFactoryMaple.scale = 0.5)

COLOR (source samples season): green (summer) / red-orange (autumn)
  season='autumn' -> warm red/orange blade; else green.

HARNESS RULES (fixed studio owns cameras/lights/floor/world/render)
  - build(seed=0) creates the leaf at the origin; returns nothing.
  - Only bpy / math / numpy / mathutils / random. Real mesh; no textures.
================================================================================
"""

import bpy
import numpy as np


def _blade_material(color):
    mat = bpy.data.materials.new("maple_leaf")
    mat.use_nodes = True
    b = mat.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*color, 1.0)
    b.inputs["Roughness"].default_value = 0.6
    if "Subsurface Weight" in b.inputs:
        b.inputs["Subsurface Weight"].default_value = 0.1
    return mat


def _fan_mesh(name, boundary):
    """Fill a (possibly concave, star-shaped) boundary with a triangle fan from the
    origin. Star-shaped-from-origin outlines (palmate lobes) fill cleanly this way."""
    verts = [(0.0, 0.0, 0.0)] + [(x, y, 0.0) for (x, y) in boundary]
    m = len(boundary)
    faces = [(0, i, i + 1) for i in range(1, m)] + [(0, m, 1)]
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.update()
    return me


def _face_up(obj):
    """Force the flat blade's normals to face +Z so the studio key light lits it."""
    if obj.data.polygons and obj.data.polygons[0].normal.z < 0:
        obj.data.flip_normals()
        obj.data.update()


def _palmate_outline(rng, n_lobes=5, sinus=0.45, serration=0.03):
    """CCW boundary of a palmate blade: each lobe = tip at full radius with a sinus
    notch to the next lobe; a small serration wobble roughens the outer edges."""
    lo, hi = np.deg2rad(18), np.deg2rad(162)
    tips = np.linspace(lo, hi, n_lobes)
    mid = (n_lobes - 1) / 2
    lens = [1.0 - 0.30 * (abs(i - mid) / mid) for i in range(n_lobes)]
    da = (tips[1] - tips[0]) * 0.42                      # half-width of a lobe, in angle
    pts = [(0.0, 0.0)]                                   # stem/base at origin
    for i in range(n_lobes):
        r = lens[i] * rng.uniform(0.95, 1.05)
        a = tips[i]
        # left shoulder -> tip -> right shoulder gives the lobe real width
        rs = r * 0.6 * (1 + serration * rng.uniform(-1, 1))
        pts.append((rs * np.cos(a - da), rs * np.sin(a - da)))
        pts.append((r * np.cos(a), r * np.sin(a)))       # lobe tip
        pts.append((rs * np.cos(a + da), rs * np.sin(a + da)))
        if i < n_lobes - 1:                              # sinus toward next lobe
            a2 = (tips[i] + tips[i + 1]) / 2
            rr = r * sinus
            pts.append((rr * np.cos(a2), rr * np.sin(a2)))
    return pts


def build(seed=0, scale=0.5, season="summer", n_lobes=5, sinus=0.45):
    rng = np.random.RandomState(seed)
    color = (0.62, 0.12, 0.05) if season == "autumn" else (0.16, 0.42, 0.13)

    me = _fan_mesh("maple_leaf", _palmate_outline(rng, n_lobes, sinus))
    obj = bpy.data.objects.new("maple_leaf", me)
    bpy.context.scene.collection.objects.link(obj)
    obj.data.materials.append(_blade_material(color))
    _face_up(obj)

    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    obj.rotation_euler.x = np.pi / 2           # stand the blade up (face toward -Y camera)
    obj.scale = (scale, scale, scale)
    bpy.ops.object.transform_apply(rotation=True, scale=True)
    bpy.ops.object.shade_flat()
    obj.name = "maple_leaf"
    return obj


if __name__ == "__main__":
    build(0)
