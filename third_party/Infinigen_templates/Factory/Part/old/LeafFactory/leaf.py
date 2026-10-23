"""
================================================================================
LeafFactory  —  a single generic LEAF blade   [PART / ELEMENT]
  faithful plain-bpy port of  <INF>/leaves/leaf.py  (LeafFactory)
================================================================================

WHAT THIS IS
  ONE leaf blade, resting flat, stem end at the origin, tip pointing +Y.
  A tree/plant [ENTRY] instances MANY of these along its outer twigs. Never pick
  this as a whole object — it is assembled INTO an [ENTRY] (see Entry/TreeFactory).

WHY A TEMPLATE (not the source)
  The Infinigen source builds the blade with numpy + low-level bpy ops and private
  helpers (mesh.select_vtx_by_idx / mesh.finalize_obj / butil.apply_transform).
  This template keeps the EXACT same silhouette math but is self-contained,
  commented, and runnable — read THIS, not the source.

SILHOUETTE MATH (identical to source, leaf.py:54-74)
  a  = linspace(0, pi, n)                         parameter along one edge
  x  = sin(a) * (leaf_width + noise*width_rand) + x_offset     half-width profile
  y  = -cos(0.9 * (a - alpha))                    length profile (alpha tilts base)
  z  = x^2 * z_scaling                            optional cupping (0 = flat)
  The blade outline = right edge (x,y,z) stem->tip + mirrored left edge (-x,y,z)
  tip->stem + one base vertex, closed into a single n-gon face, then a WAVE
  modifier gives it a natural ripple.
  (Source reached this outline via primitive_circle+subdivide ops; we build the
  SAME vertices directly with from_pydata — clearer and op-independent.)

KNOBS (the genome dict — same names as source)
  leaf_width  0.5   half-width of the blade (bigger = rounder/fatter leaf)
  alpha       0.3   base-angle tilt; larger = more heart/notched base
  z_scaling   0.0   cupping out of plane; >0 curls the blade into a shallow cup
  use_wave    True   natural edge ripple (WAVE modifier)
  width_rand  0.33  per-leaf random width variation
  flip_leaf   False  mirror the asymmetry
  scale       0.3   final size in metres (a leaf ~0.3 m tip-to-stem before tree scales)

HOW OTHER LEAF SPECIES RELATE (all live under Factory/Part/)
  This generic ovate blade is the base. Broadleaf / Maple / Ginko / Pine / V2 are
  separate [PART] templates with their own outline; a maple is palmate (lobed),
  a ginkgo is a fan, a pine is a needle cluster. Same instancing story.

HARNESS RULES (fixed studio owns cameras/lights/floor/world/render)
  - build(seed=0) creates the leaf at the origin; returns nothing.
  - Only bpy / math / numpy / mathutils / random. No image textures.
  - Real mesh (not particle-hair) so it renders when a tree joins many copies.
================================================================================
"""

import bpy
import numpy as np


# ---------------------------------------------------------------------------
#  leaf material (green blade with subtle noise variation)
# ---------------------------------------------------------------------------
def _leaf_material(name="leaf", base=(0.14, 0.42, 0.12)):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (*base, 1.0)
    bsdf.inputs["Roughness"].default_value = 0.6
    if "Subsurface Weight" in bsdf.inputs:
        bsdf.inputs["Subsurface Weight"].default_value = 0.1
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 6.0
    noise.inputs["Detail"].default_value = 4.0
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].color = (0.10, 0.32, 0.08, 1.0)
    ramp.color_ramp.elements[1].color = (0.30, 0.55, 0.16, 1.0)
    nt.links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
    nt.links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
    return mat


# ---------------------------------------------------------------------------
#  build(seed) — port of LeafFactory.create_asset
# ---------------------------------------------------------------------------
def build(seed=0, genome=None, scale=0.3, n=16):
    rng = np.random.RandomState(seed)
    G = dict(leaf_width=0.5, alpha=0.3, use_wave=True, x_offset=0.0,
             flip_leaf=False, z_scaling=0.0, width_rand=0.33)
    if genome:
        G.update(genome)

    # 1. silhouette coords (leaf.py:54-74) — right edge + mirrored left edge + base
    a = np.linspace(0, np.pi, n)
    if G["flip_leaf"]:
        a = a[::-1]
    x = np.sin(a) * (G["leaf_width"] + rng.randn() * G["width_rand"]) + G["x_offset"]
    y = -np.cos(0.9 * (a - G["alpha"]))
    z = x ** 2 * G["z_scaling"]
    verts = np.concatenate([
        np.stack([x, y, z], 1),                       # right edge, stem->tip  (0..n-1)
        np.stack([-x[::-1], y[::-1], z], 1),          # left edge, tip->stem   (n..2n-1)
        np.array([[0.0, y[0], 0.0]]),                 # single base vertex     (2n)
    ])
    verts = [tuple(v) for v in verts]
    face = list(range(len(verts)))                    # one n-gon around the outline

    # 2. build the blade mesh directly
    me = bpy.data.meshes.new("leaf")
    me.from_pydata(verts, [], [face])
    me.update()
    obj = bpy.data.objects.new("leaf", me)
    bpy.context.scene.collection.objects.link(obj)

    # 3. natural edge ripple
    if G["use_wave"]:
        w = obj.modifiers.new("Wave", "WAVE")
        w.height = rng.randn() * 0.3
        w.width = 0.75 + rng.randn() * 0.1
        w.speed = rng.rand()

    # 4. shade, material, scale, origin at the stem (base vertex)
    obj.data.materials.append(_leaf_material())
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.modifier_apply(modifier="Wave") if G["use_wave"] else None
    bpy.ops.object.shade_smooth()
    obj.scale = (scale, scale, scale)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    return obj


if __name__ == "__main__":
    build(0)
