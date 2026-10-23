"""
================================================================================
LeafFactoryGinko  —  a fan-shaped GINKGO leaf   [PART / ELEMENT]
  readable plain-bpy port of  <INF>/leaves/leaf_ginko.py  (LeafFactoryGinko)
================================================================================

WHAT THIS IS
  ONE ginkgo leaf: the unmistakable flat fan (broad curved outer margin, narrow
  wedge to the stem, often a small central notch), stem at the origin, fan opening
  toward +Y, lying flat (normal +Z). A ginkgo tree [ENTRY] instances MANY.

WHY A TEMPLATE (not the source)
  Source ~grows the fan from a vein contour in Geometry Nodes. The SILHOUETTE that
  matters: a circular sector (fan) with a small stem wedge and an optional notch at
  the outer centre. Reproduced parametrically in plain bpy.

SPECIES OUTLINE
  half_angle  55deg  half the fan opening angle
  notch       0.12   depth of the central cleft in the outer margin (0 = none)
  scale       0.4    leaf size in metres

COLOR  green (summer) / golden yellow (autumn — the classic ginkgo colour)

HARNESS RULES (fixed studio owns cameras/lights/floor/world/render)
  - build(seed=0) creates the leaf at the origin; returns nothing.
  - Only bpy / math / numpy / mathutils / random. Real mesh; no textures.
================================================================================
"""

import bpy
import numpy as np


def _blade_material(color):
    mat = bpy.data.materials.new("ginko")
    mat.use_nodes = True
    b = mat.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*color, 1.0)
    b.inputs["Roughness"].default_value = 0.55
    if "Subsurface Weight" in b.inputs:
        b.inputs["Subsurface Weight"].default_value = 0.1
    return mat


def _face_up(obj):
    if obj.data.polygons and obj.data.polygons[0].normal.z < 0:
        obj.data.flip_normals()
        obj.data.update()


def build(seed=0, scale=0.4, season="summer", half_angle=55.0, notch=0.12, n=20):
    rng = np.random.RandomState(seed)
    color = (0.92, 0.78, 0.10) if season == "autumn" else (0.28, 0.5, 0.18)

    ha = np.deg2rad(half_angle)
    # outer curved margin: sweep the fan angle at radius 1, dipping at the centre notch
    ang = np.linspace(np.pi / 2 - ha, np.pi / 2 + ha, n)   # centred on +Y
    r = 1.0 - notch * np.exp(-((ang - np.pi / 2) / (0.25 * ha)) ** 2)  # central cleft
    outer = [(ri * np.cos(a), ri * np.sin(a)) for a, ri in zip(ang, r)]
    # triangle-fan the fan sector from the stem at the origin -> clean fill
    verts = [(0.0, 0.0, 0.0)] + [(x, y, 0.0) for (x, y) in outer]
    faces = [(0, i, i + 1) for i in range(1, len(outer))]
    me = bpy.data.meshes.new("ginko")
    me.from_pydata(verts, [], faces)
    me.update()
    obj = bpy.data.objects.new("ginko", me)
    bpy.context.scene.collection.objects.link(obj)
    obj.data.materials.append(_blade_material(color))
    _face_up(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    obj.rotation_euler.x = np.pi / 2           # stand the blade up (face toward -Y camera)
    obj.scale = (scale, scale, scale)
    bpy.ops.object.transform_apply(rotation=True, scale=True)
    bpy.ops.object.shade_flat()
    obj.name = "ginko"
    return obj


if __name__ == "__main__":
    build(0)
