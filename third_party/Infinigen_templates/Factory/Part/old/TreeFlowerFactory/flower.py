"""
================================================================================
TreeFlowerFactory  —  a single BLOSSOM   [PART / ELEMENT]
  readable plain-bpy port of  <INF>/trees/tree_flower.py  (TreeFlowerFactory)
================================================================================

WHAT THIS IS
  ONE small flower/blossom (cherry, apple, plum...), lying face-up, its base at the
  origin, opening toward +Z. A spring/flowering tree [ENTRY] instances MANY of these
  on its outer twigs INSTEAD of leaves (generate.py: season=='spring' -> 'flower').
  Never pick this as a whole object — it is assembled INTO a tree [ENTRY].

WHY A TEMPLATE (not the source)
  The source is ~950 lines of Geometry-Nodes DSL (phyllotaxis node group, petal
  node group, seed instancing, translucent shader). This template reproduces the
  SAME structure — a flat center disk + N radially-placed tapered petals — as clear,
  runnable plain bpy. Read THIS.

STRUCTURE (matches geo_flower + get_flower_params in source)
  overall_rad     0.15-0.25  whole-flower radius (source: rad=uniform(0.15,0.25))
  pct_inner       0.05-0.4   fraction that is the seed CENTER disk
  Center Rad      = overall_rad * pct_inner            flat UV-sphere disk (z*0.05)
  Petal length    = overall_rad * (1 - pct_inner)      Petal Dims X
  base_width      2*pi*rad*pct_inner / ~20             petal width at the root  (Y)
  top_width       ~0.7 * rad                           petal width near the tip  (Z)
  petal count     = round(2*pi*CenterRad / base_width * 1.2)   petals packed around
  petal angle     min..max in deg(-20..100)            how far petals tilt UP/open
  curl            deg(normal(30,50))                   lengthwise petal curl

COLORS (source shaders)
  petals  translucent pink/white   (colors.tree_petal_hsv)  -> shader_petal
  center  dark->orange gradient                             -> shader_flower_center

KNOBS you will actually turn for a species
  rad      bigger flower vs small blossom
  color    petal RGB (pink cherry, white pear, yellow...)
  n_petal  override the computed count (5 = classic cherry/apple blossom)
  openness petal tilt: 0 = closed cup, 1 = flat open

HARNESS RULES (fixed studio owns cameras/lights/floor/world/render)
  - build(seed=0) creates the flower at the origin; returns nothing.
  - Only bpy / math / numpy / mathutils / random. Real joined mesh. No textures.
================================================================================
"""

import bpy
import numpy as np
from mathutils import Matrix, Vector


# ---------------------------------------------------------------------------
#  materials
# ---------------------------------------------------------------------------
def _petal_material(color=(0.95, 0.55, 0.65)):
    mat = bpy.data.materials.new("petal")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (*color, 1.0)
    bsdf.inputs["Roughness"].default_value = 0.55
    if "Subsurface Weight" in bsdf.inputs:          # petals are translucent
        bsdf.inputs["Subsurface Weight"].default_value = 0.35
        bsdf.inputs["Subsurface Radius"].default_value = (0.3, 0.15, 0.2)
    return mat


def _center_material(color=(0.95, 0.62, 0.11)):
    mat = bpy.data.materials.new("flower_center")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (*color, 1.0)
    bsdf.inputs["Roughness"].default_value = 0.7
    return mat


# ---------------------------------------------------------------------------
#  one tapered petal, root at origin, growing along +Y, opening in +Z
#     width goes base_width (root) -> top_width (point) -> 0 (tip), with curl
# ---------------------------------------------------------------------------
def _petal_mesh(rng, length, base_width, top_width, point=0.56, curl=0.5,
                res_v=10, res_h=4):
    verts, faces = [], []
    for j in range(res_v + 1):
        v = j / res_v                                 # 0 root -> 1 tip
        # width profile: base -> top near `point`, then taper to 0 at the tip
        if v <= point:
            w = base_width + (top_width - base_width) * (v / point)
        else:
            w = top_width * (1 - (v - point) / (1 - point))
        y = v * length
        z = curl * (v ** 2) * length * 0.25           # lengthwise curl upward
        row = []
        for i in range(res_h + 1):
            u = (i / res_h - 0.5)                      # -0.5 .. 0.5 across width
            cup = (1 - (2 * u) ** 2) * 0.06 * w        # slight cross-section cup
            row.append(len(verts))
            verts.append((u * w, y, z + cup))
        if j > 0:
            p = [r for r in range(len(verts) - 2 * (res_h + 1), len(verts))]
            a, b = p[:res_h + 1], p[res_h + 1:]
            for i in range(res_h):
                faces.append((a[i], a[i + 1], b[i + 1], b[i]))
    me = bpy.data.meshes.new("petal")
    me.from_pydata(verts, [], faces)
    me.update()
    return me


# ---------------------------------------------------------------------------
#  build(seed) — port of TreeFlowerFactory.create_asset + get_flower_params
# ---------------------------------------------------------------------------
def build(seed=0, rad=None, color=(0.95, 0.55, 0.65), n_petal=None, openness=0.6):
    rng = np.random.RandomState(seed)
    overall_rad = rad if rad is not None else rng.uniform(0.15, 0.25)
    pct_inner = rng.uniform(0.05, 0.4)
    center_rad = overall_rad * pct_inner
    petal_len = overall_rad * (1 - pct_inner)
    base_width = max(2 * np.pi * center_rad / 20.0, 0.01)
    top_width = float(np.clip(overall_rad * 0.7, base_width * 1.2, 100))
    curl = np.deg2rad(rng.normal(30, 30))

    # petal count: packed around the center circumference (source multiply_3)
    if n_petal is None:
        n_petal = int(round(2 * np.pi * center_rad / base_width * 1.2))
    n_petal = max(4, min(n_petal, 12))

    # petal elevation above the plane: openness 0 = nearly upright cup,
    # openness 1 = flat open blossom. Petals always rise (+z), never sink.
    elevation = np.deg2rad(10 + 70 * (1 - openness))  # 80deg (cup) .. 10deg (flat)

    petal_mat = _petal_material(color)
    petals = []
    for k in range(n_petal):
        me = _petal_mesh(rng, petal_len, base_width, top_width, curl=curl)
        obj = bpy.data.objects.new("petal", me)
        bpy.context.scene.collection.objects.link(obj)
        obj.data.materials.append(petal_mat)
        az = 2 * np.pi * k / n_petal                  # even radial spread
        # raise the petal above the plane (X axis), then swing it to its azimuth
        R = Matrix.Rotation(az, 4, 'Z') @ Matrix.Rotation(elevation, 4, 'X')
        loc = Matrix.Translation((center_rad * np.cos(az + np.pi / 2),
                                  center_rad * np.sin(az + np.pi / 2), 0.0))
        obj.matrix_world = loc @ R
        petals.append(obj)

    # flat center disk (uv sphere squashed on z, source Transform scale z=0.05)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=12, ring_count=8, radius=center_rad)
    center = bpy.context.active_object
    center.scale = (1, 1, 0.08)
    bpy.ops.object.transform_apply(scale=True)
    center.name = "flower_center"
    center.data.materials.append(_center_material())

    # join everything into one flower
    bpy.ops.object.select_all(action='DESELECT')
    for o in petals:
        o.select_set(True)
    center.select_set(True)
    bpy.context.view_layer.objects.active = center
    bpy.ops.object.join()
    flower = bpy.context.active_object
    flower.name = "flower"
    flower.rotation_euler.z = rng.uniform(0, 2 * np.pi)
    bpy.ops.object.shade_smooth()
    return flower


if __name__ == "__main__":
    build(0)
