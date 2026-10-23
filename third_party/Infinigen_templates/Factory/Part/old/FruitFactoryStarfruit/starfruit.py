"""
================================================================================
FruitFactoryStarfruit  —  a starfruit   [PART / ELEMENT]
  readable plain-bpy port of  <INF>/fruits/starfruit.py  (+ general_fruit.py engine)
================================================================================

WHAT THIS IS
  ONE starfruit. A tree/plant [ENTRY] instances a few of these near its branch tips
  (generate.py fruit dispatch, fruit_scale=0.2). Never pick it as a whole object.

WHY A TEMPLATE (not the source)
  In Infinigen every fruit = the shared FruitFactoryGeneralFruit engine + 4 param
  samplers (cross_section x shape x surface x stem). This file carries ONLY this
  fruit's params and inlines a compact copy of the engine so it runs alone. For the
  full engine + all knobs see ../FruitFactoryGeneralFruit/general_fruit.py.

WHAT MAKES IT LOOK REAL (ported from the source, not simplified away)
  - SMOOTH silhouette: the radius profile is run through a Catmull-Rom spline
    (source shape_quadratic is a Bezier), NOT a polyline -> a round fruit, no facets.
  - MOTTLED skin: two colours blended by noise (source apple_surface color1/color2),
    not a flat plastic colour.
  - CALYX dimples pressed into both poles (source add_dent).

THIS FRUIT (from starfruit.py)
  starfruit: 5 sharp longitudinal ridges -> a star in cross-section; waxy yellow
  radius profile r(t), t bottom->top: [(0.0727, 0.2), (0.2636, 0.6063), (0.55, 0.8), (0.8886, 0.6094), (1, 0)]
  cross-section: star   base colour (0.9, 0.78, 0.1)   blush colour (0.66, 0.74, 0.1)

HARNESS RULES (fixed studio owns cameras/lights/floor/world/render)
  - build(seed=0) creates the fruit at the origin resting on z=0; returns nothing.
  - Only bpy / math / numpy / mathutils / random. Real joined mesh; no image textures.
================================================================================
"""

import bpy
import numpy as np

# ---- this fruit's parameters (from starfruit.py sample_* methods) ----------------
PROFILE = [(0.0727, 0.2), (0.2636, 0.6063), (0.55, 0.8), (0.8886, 0.6094), (1, 0)]
COLOR = (0.9, 0.78, 0.1)          # base skin colour
COLOR2 = (0.66, 0.74, 0.1)        # blush/patch colour blended in by noise
CROSS = "star"        # ring outline: circle / star / coconut
STAR_AMP = 0.5
N_STAR = 5
HAS_STEM = True
BUMP_SCALE = 20   # surface_bump noise scale
BUMP_DISP = 0.015    # surface_bump displacement along normals


def _profile_samples(cp, n):
    """Catmull-Rom spline through the (t, r) control points -> smooth r(t)."""
    cp = np.array(sorted(cp), float)
    t_cp, r_cp = cp[:, 0], cp[:, 1]
    tp = np.concatenate([[t_cp[0]-(t_cp[1]-t_cp[0])], t_cp, [t_cp[-1]+(t_cp[-1]-t_cp[-2])]])
    rp = np.concatenate([[r_cp[0]], r_cp, [r_cp[-1]]])
    ts = np.linspace(t_cp[0], t_cp[-1], n)
    rs = np.empty(n)
    for k, t in enumerate(ts):
        i = min(max(np.searchsorted(t_cp, t) - 1, 0), len(t_cp) - 2)
        seg = tp[i+1:i+3]
        u = 0.0 if seg[1] == seg[0] else (t - seg[0]) / (seg[1] - seg[0])
        p0, p1, p2, p3 = rp[i], rp[i+1], rp[i+2], rp[i+3]
        rs[k] = 0.5 * (2*p1 + (-p0+p2)*u + (2*p0-5*p1+4*p2-p3)*u*u
                       + (-p0+3*p1-3*p2+p3)*u*u*u)
    return np.linspace(0, 1, n), np.clip(rs, 0.0, None)


def _material(color, color2):
    """Two-colour noise-mottled skin (port of apple_surface shader)."""
    mat = bpy.data.materials.new("fruit"); mat.use_nodes = True
    nt = mat.node_tree; bsdf = nt.nodes["Principled BSDF"]
    bsdf.inputs["Roughness"].default_value = 0.35
    tc = nt.nodes.new("ShaderNodeTexCoord")
    patch = nt.nodes.new("ShaderNodeTexNoise")
    patch.inputs["Scale"].default_value = 1.4; patch.inputs["Detail"].default_value = 2.0
    nt.links.new(tc.outputs["Object"], patch.inputs["Vector"])
    bias = nt.nodes.new("ShaderNodeMath"); bias.operation = "MULTIPLY"
    bias.inputs[1].default_value = 0.5
    nt.links.new(patch.outputs["Fac"], bias.inputs[0])
    mix1 = nt.nodes.new("ShaderNodeMixRGB")
    mix1.inputs["Color1"].default_value = (*color, 1.0)
    mix1.inputs["Color2"].default_value = (*color2, 1.0)
    nt.links.new(bias.outputs["Value"], mix1.inputs["Fac"])
    fine = nt.nodes.new("ShaderNodeTexNoise")
    fine.inputs["Scale"].default_value = 12.0; fine.inputs["Detail"].default_value = 8.0
    nt.links.new(tc.outputs["Object"], fine.inputs["Vector"])
    hsv = nt.nodes.new("ShaderNodeHueSaturation")
    hsv.inputs["Hue"].default_value = 0.5; hsv.inputs["Value"].default_value = 0.75
    nt.links.new(mix1.outputs["Color"], hsv.inputs["Color"])
    speck = nt.nodes.new("ShaderNodeMath"); speck.operation = "MULTIPLY"
    speck.inputs[1].default_value = 0.15
    nt.links.new(fine.outputs["Fac"], speck.inputs[0])
    mix2 = nt.nodes.new("ShaderNodeMixRGB")
    nt.links.new(speck.outputs["Value"], mix2.inputs["Fac"])
    nt.links.new(mix1.outputs["Color"], mix2.inputs["Color1"])
    nt.links.new(hsv.outputs["Color"], mix2.inputs["Color2"])
    nt.links.new(mix2.outputs["Color"], bsdf.inputs["Base Color"])
    return mat


def _lathe(cp, color, color2, cross, star_amp, n_star, cross_radius=1.5,
           height=2.0, seg=64, n_ring=96, dent=0.12):
    ts, rs = _profile_samples(cp, n_ring); rs = rs * cross_radius
    th = np.linspace(0, 2*np.pi, seg, endpoint=False)
    if cross == "star":
        cf = 1.0 + star_amp * np.cos(n_star * th)
    elif cross == "coconut":
        cf = 1.0 + 0.06 * np.cos(3 * th)
    else:
        cf = np.ones_like(th)
    zt = ts * height + dent*height*(np.exp(-(ts/0.10)**2) - np.exp(-((1-ts)/0.10)**2))
    verts, faces = [], []
    for j in range(n_ring):
        base = len(verts)
        for i in range(seg):
            verts.append((rs[j]*cf[i]*np.cos(th[i]), rs[j]*cf[i]*np.sin(th[i]), zt[j]))
        if j > 0:
            p = base - seg
            for i in range(seg):
                faces.append((p+i, p+(i+1)%seg, base+(i+1)%seg, base+i))
    me = bpy.data.meshes.new("fruit"); me.from_pydata(verts, [], faces); me.update()
    obj = bpy.data.objects.new("fruit", me)
    bpy.context.scene.collection.objects.link(obj)
    obj.data.materials.append(_material(color, color2))
    bpy.context.view_layer.objects.active = obj; obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.remove_doubles(threshold=1e-5)
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode="OBJECT")
    if BUMP_DISP > 0:
        _surface_bump(obj, BUMP_SCALE, BUMP_DISP)
    bpy.ops.object.shade_smooth()
    return obj, float(zt[-1])   # true top-of-body z (after dent) for stem seating



def _surface_bump(obj, scale, disp):
    """Port of fruit_utils nodegroup_surface_bump: push every vertex along its
    normal by noise(pos*scale)*disp -> real bumpy skin relief (all fruit surfaces
    use this). mathutils.noise.noise gives the Perlin field the source samples."""
    from mathutils import noise as _n
    me = obj.data
    me.calc_normals_split() if hasattr(me, "calc_normals_split") else None
    for v in me.vertices:
        f = _n.noise(v.co * scale)
        v.co = v.co + v.normal * (f * disp)
    me.update()


def _stem(top_z, color=(0.28, 0.16, 0.06), stem_len=0.5):
    cu = bpy.data.curves.new("stem", "CURVE")
    cu.dimensions = "3D"; cu.bevel_depth = 1.0; cu.resolution_u = 3
    sp = cu.splines.new("POLY"); sp.points.add(5)
    for i in range(6):
        t = i/5
        sp.points[i].co = (0.06*np.sin(t*2), 0.0, top_z + t*stem_len, 1.0)
        sp.points[i].radius = 0.05*(1 - 0.4*t)
    obj = bpy.data.objects.new("stem", cu)
    bpy.context.scene.collection.objects.link(obj)
    m = bpy.data.materials.new("stem"); m.use_nodes = True
    m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (*color, 1.0)
    obj.data.materials.append(m)
    bpy.context.view_layer.objects.active = obj; obj.select_set(True)
    bpy.ops.object.convert(target="MESH")
    return bpy.context.active_object


def build(seed=0):
    np.random.seed(seed)
    obj, top = _lathe(PROFILE, COLOR, COLOR2, CROSS, STAR_AMP, N_STAR)
    parts = [obj]
    if HAS_STEM:
        parts.append(_stem(top))
    bpy.ops.object.select_all(action="DESELECT")
    for o in parts:
        o.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.join()
    fr = bpy.context.active_object; fr.name = "starfruit"
    fr.location.z -= min(v.co.z for v in fr.data.vertices)
    bpy.ops.object.transform_apply(location=True)


if __name__ == "__main__":
    build(0)
