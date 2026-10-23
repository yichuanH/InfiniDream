"""
================================================================================
FruitFactoryGeneralFruit  —  the shared FRUIT ENGINE   [PART / ELEMENT / ENGINE]
  readable plain-bpy port of  <INF>/fruits/general_fruit.py  (FruitFactoryGeneralFruit)
================================================================================

WHAT THIS IS
  The engine EVERY fruit is built from. In Infinigen a concrete fruit (apple,
  strawberry, durian...) is JUST four parameter samplers plugged into this engine:
      cross_section  x  shape(radius profile)  x  surface(colour)  x  stem
  apple.py / strawberry.py / ... only override those four; the geometry lives here.

THE RECIPE (surface of revolution + stem)
  1. SHAPE  = a radius profile r(t), t in [0,1] bottom->top, given as a few control
     points (shape_quadratic radius_control_points). This profile IS the fruit
     silhouette: apple bulges in the middle and dimples at both poles.
  2. CROSS SECTION = how the ring looks in plan: a circle (most fruits), a 5-point
     STAR (starfruit), etc. Modulates the ring radius with angle.
  3. Revolve the profile around the Z axis -> a closed fruit body (r=0 at both ends
     gives clean poles). Apply the fruit colour.
  4. STEM = a short curved twig at the top (Translation z~0.6 in source).

USE THIS to build any fruit: call `build_fruit(profile, color, ...)` with that
fruit's control points + colour. See the sibling fruit templates (apple.py etc.)
for the exact numbers per species; each maps a name -> these params (source
generate.py fruit dispatch, fruit_scale=0.2 when hung on a tree).

HARNESS RULES (fixed studio owns cameras/lights/floor/world/render)
  - build(seed=0) creates a fruit at the origin resting on z=0; returns nothing.
  - Only bpy / math / numpy / mathutils / random. Real joined mesh; no textures.
================================================================================
"""

import bpy
import numpy as np
from mathutils import Vector


# ---------------------------------------------------------------------------
#  materials
# ---------------------------------------------------------------------------
def fruit_material(name, color, color2=None, roughness=0.35, seed=0.0):
    """Procedural mottled skin — a plain-node port of Infinigen's apple_surface
    shader (surfaces/apple_surface.py): two colours blended by a large-scale noise
    (patches) with a second fine noise adding a hue-shifted speckle. color2 defaults
    to a slightly warmer/darker variant of color so any fruit gets natural variation
    instead of a flat plastic colour."""
    if color2 is None:
        color2 = tuple(min(1.0, c * 1.25 + 0.04) for c in color)
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    bsdf.inputs["Roughness"].default_value = roughness
    if "Subsurface Weight" in bsdf.inputs:
        bsdf.inputs["Subsurface Weight"].default_value = 0.05

    tc = nt.nodes.new("ShaderNodeTexCoord")
    # large-scale patch noise -> mix the two skin colours (apple_surface musgrave 0.6)
    patch = nt.nodes.new("ShaderNodeTexNoise")
    patch.inputs["Scale"].default_value = 1.4 + seed * 0.0
    patch.inputs["Detail"].default_value = 2.0
    nt.links.new(tc.outputs["Object"], patch.inputs["Vector"])
    # bias the blend toward color (the base skin); color2 only shows in patches
    bias = nt.nodes.new("ShaderNodeMath")
    bias.operation = "MULTIPLY"
    bias.inputs[1].default_value = 0.5
    nt.links.new(patch.outputs["Fac"], bias.inputs[0])
    mix1 = nt.nodes.new("ShaderNodeMixRGB")
    mix1.inputs["Color1"].default_value = (*color, 1.0)
    mix1.inputs["Color2"].default_value = (*color2, 1.0)
    nt.links.new(bias.outputs["Value"], mix1.inputs["Fac"])
    # fine speckle: a hue-shifted copy mixed in by a high-freq noise (musgrave 10)
    fine = nt.nodes.new("ShaderNodeTexNoise")
    fine.inputs["Scale"].default_value = 12.0
    fine.inputs["Detail"].default_value = 8.0
    nt.links.new(tc.outputs["Object"], fine.inputs["Vector"])
    hsv = nt.nodes.new("ShaderNodeHueSaturation")
    hsv.inputs["Hue"].default_value = 0.5          # subtle value/tone shift, same hue family
    hsv.inputs["Value"].default_value = 0.75
    nt.links.new(mix1.outputs["Color"], hsv.inputs["Color"])
    speck = nt.nodes.new("ShaderNodeMath")         # keep the speckle very subtle
    speck.operation = "MULTIPLY"
    speck.inputs[1].default_value = 0.15
    nt.links.new(fine.outputs["Fac"], speck.inputs[0])
    mix2 = nt.nodes.new("ShaderNodeMixRGB")
    nt.links.new(speck.outputs["Value"], mix2.inputs["Fac"])
    nt.links.new(mix1.outputs["Color"], mix2.inputs["Color1"])
    nt.links.new(hsv.outputs["Color"], mix2.inputs["Color2"])
    nt.links.new(mix2.outputs["Color"], bsdf.inputs["Base Color"])
    return mat


def stem_material(color=(0.28, 0.16, 0.06)):
    mat = bpy.data.materials.new("stem")
    mat.use_nodes = True
    b = mat.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*color, 1.0)
    b.inputs["Roughness"].default_value = 0.8
    return mat


# ---------------------------------------------------------------------------
#  lathe: revolve a radius profile r(t) around Z, with a cross-section modulation
# ---------------------------------------------------------------------------
def _profile_samples(control_points, n):
    """Resample the few (t, r) control points into a SMOOTH r(t) over n rings.
    Infinigen's shape_quadratic runs a Bezier/spline through the control points, NOT
    a polyline — using a Catmull-Rom spline here rounds the silhouette (an apple, not
    a truncated cone). Linear interpolation is what makes a lathed fruit look faceted."""
    cp = np.array(sorted(control_points), dtype=float)
    t_cp, r_cp = cp[:, 0], cp[:, 1]
    # pad ends for Catmull-Rom tangents
    tp = np.concatenate([[t_cp[0] - (t_cp[1] - t_cp[0])], t_cp,
                         [t_cp[-1] + (t_cp[-1] - t_cp[-2])]])
    rp = np.concatenate([[r_cp[0]], r_cp, [r_cp[-1]]])
    ts = np.linspace(t_cp[0], t_cp[-1], n)
    rs = np.empty(n)
    for k, t in enumerate(ts):
        i = np.searchsorted(t_cp, t) - 1
        i = min(max(i, 0), len(t_cp) - 2)
        seg = tp[i + 1:i + 3]
        u = 0.0 if seg[1] == seg[0] else (t - seg[0]) / (seg[1] - seg[0])
        p0, p1, p2, p3 = rp[i], rp[i + 1], rp[i + 2], rp[i + 3]
        u2 = u * u
        u3 = u2 * u
        rs[k] = 0.5 * ((2 * p1) + (-p0 + p2) * u
                       + (2 * p0 - 5 * p1 + 4 * p2 - p3) * u2
                       + (-p0 + 3 * p1 - 3 * p2 + p3) * u3)
    rs = np.clip(rs, 0.0, None)
    # full-length t axis 0..1 for z mapping regardless of the first control t
    ts01 = np.linspace(0, 1, n)
    return ts01, rs


def _cross_factor(theta, cross="circle", n_star=5, star_amp=0.4):
    """Ring-radius multiplier as a function of angle -> the plan-view outline."""
    if cross == "star":
        return 1.0 + star_amp * np.cos(n_star * theta)     # ridged (starfruit)
    if cross == "coconut":
        return 1.0 + 0.06 * np.cos(3 * theta)              # gently 3-sided
    return np.ones_like(theta)                              # circle


def build_fruit(control_points, color, color2=None, cross="circle", n_star=5,
                star_amp=0.45, cross_radius=1.5, height=2.0, seg=64, n_ring=96,
                roughness=0.35, dent=0.12, bump_scale=28.0, bump_disp=0.012,
                stem=True, stem_color=(0.28, 0.16, 0.06), stem_len=0.5, name="fruit"):
    """Revolve `control_points` (list of (t, r)) around Z into a fruit body.
    cross_radius = the circle_cross_section radius (source ~1.5) scaling the whole
    body width, so the fruit is as wide as it is tall for typical profiles.
    dent = depth of the calyx wells pressed into the top & bottom poles (source
    apple_surface add_dent) — what gives an apple its dimples instead of a smooth ball."""
    ts, rs = _profile_samples(control_points, n_ring)
    rs = rs * cross_radius
    thetas = np.linspace(0, 2 * np.pi, seg, endpoint=False)
    cf = _cross_factor(thetas, cross, n_star, star_amp)
    # press a concave well into each pole: pull the last/first rings' z toward centre
    zt = ts * height
    well = dent * height * (np.exp(-(ts / 0.10) ** 2) - np.exp(-((1 - ts) / 0.10) ** 2))
    zt = zt + well

    verts, faces = [], []
    for j in range(n_ring):
        z = zt[j]
        r = rs[j]
        base = len(verts)
        for i in range(seg):
            x = r * cf[i] * np.cos(thetas[i])
            y = r * cf[i] * np.sin(thetas[i])
            verts.append((x, y, z))
        if j > 0:
            prev = base - seg
            for i in range(seg):
                a, b = prev + i, prev + (i + 1) % seg
                c, d = base + (i + 1) % seg, base + i
                faces.append((a, b, c, d))
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.update()
    obj = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(obj)
    obj.data.materials.append(fruit_material(name, color, color2, roughness))

    # weld the seam + smooth + close the poles with normals
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.remove_doubles(threshold=1e-5)
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode="OBJECT")
    if bump_disp > 0:
        surface_bump(obj, bump_scale, bump_disp)
    bpy.ops.object.shade_smooth()

    parts = [obj]
    if stem:
        parts.append(_stem(height, stem_len, stem_color))

    bpy.ops.object.select_all(action="DESELECT")
    for o in parts:
        o.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.join()
    fruit = bpy.context.active_object
    fruit.name = name
    # rest on z=0
    zmin = min((fruit.matrix_world @ v.co).z for v in fruit.data.vertices)
    fruit.location.z -= zmin
    bpy.ops.object.transform_apply(location=True)
    return fruit


def surface_bump(obj, scale=50.0, displacement=0.02):
    """Port of fruit_utils nodegroup_surface_bump: displace every vertex along its
    normal by noise(pos*scale)*displacement -> real bumpy skin relief. EVERY fruit
    surface shader applies this; tune (scale, displacement) per fruit (blackberry big
    lumps, coconuthairy fine fibres, durian spikes)."""
    from mathutils import noise as _n
    me = obj.data
    for v in me.vertices:
        v.co = v.co + v.normal * (_n.noise(v.co * scale) * displacement)
    me.update()


def _stem(height, stem_len, color):
    """Short curved stem rising from the top pole (source basic_stem)."""
    cu = bpy.data.curves.new("stem", 'CURVE')
    cu.dimensions = '3D'
    cu.bevel_depth = 1.0
    cu.resolution_u = 3
    sp = cu.splines.new('POLY')
    steps = 6
    sp.points.add(steps - 1)
    for i in range(steps):
        t = i / (steps - 1)
        sp.points[i].co = (0.06 * np.sin(t * 2), 0.0, height + t * stem_len, 1.0)
        sp.points[i].radius = 0.05 * (1 - 0.4 * t)
    obj = bpy.data.objects.new("stem", cu)
    bpy.context.scene.collection.objects.link(obj)
    obj.data.materials.append(stem_material(color))
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.convert(target='MESH')
    return bpy.context.active_object


# ---------------------------------------------------------------------------
#  build(seed) — a generic rounded fruit (the engine's default body)
# ---------------------------------------------------------------------------
def build(seed=0):
    rng = np.random.RandomState(seed)
    profile = [(0.0, 0.0), (0.12, 0.43), (0.47, 0.66), (0.89, 0.42), (1.0, 0.0)]
    # two skin colours -> red apple with green/yellow blush patches
    # (source apple_surface color1/color2 blended by noise)
    build_fruit(profile, color=(0.60, 0.05, 0.04), color2=(0.42, 0.40, 0.06),
                name="fruit")


if __name__ == "__main__":
    build(0)
