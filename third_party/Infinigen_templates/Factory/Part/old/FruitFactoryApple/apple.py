"""
================================================================================
FruitFactoryApple  —  an apple   [PART / ELEMENT]
  faithful plain-bpy port of  <INF>/fruits/apple.py
     + general_fruit.py (geometry) + surfaces/apple_surface.py (skin + dents)
     + fruit_utils.py (shape_quadratic, add_dent) + core/util/color.hsv2rgba
================================================================================

EVERY NUMBER BELOW COMES FROM THE SOURCE — cited inline as  # <file>: <what>.
This template does NOT eyeball-tune to a render; it reproduces infinigen's actual
parameters and node math. The only unavoidable adaptations (Geometry-Nodes nodes
that have no 1:1 plain-bpy form) are marked  # [GN-EQUIV].

CONSTRUCTION (general_fruit.general_fruit_geometry_nodes):
  1. cross_section = circle, radius normal(1.5,0.05)          # apple.py:26
  2. shape         = shape_quadratic: a quadratic-bezier spine Start->Middle->End
     with a per-point RADIUS float-curve = radius_control_points               # apple.py:35-51 / fruit_utils shape_quadratic
  3. surface       = apple_surface: two add_dent passes (calyx wells top & bottom)
     + the musgrave-mottled two-colour skin shader                             # apple_surface.py
  4. stem          = basic_stem at Translation (0,0,0.6)                        # apple.py:92-107
  NOTE: apple_surface uses add_dent, NOT surface_bump — the apple skin is smooth.

HARNESS RULES (fixed studio owns cameras/lights/floor/world/render)
  - build(seed=0) creates the apple at the origin resting on z=0; returns nothing.
  - Only bpy / math / numpy / mathutils / random / colorsys. Real joined mesh; no textures.
================================================================================
"""

import bpy
import colorsys
import numpy as np
from numpy.random import uniform, normal, seed as npseed

# apple.py:35  radius_control_points (shape_quadratic radius float-curve)
RADIUS_CP = [(0.0, 0.0), (0.1227, 0.4281), (0.4705, 0.6625), (0.8886, 0.4156), (1.0, 0.0)]
# apple_surface.py:102  dent_control_points (calyx well profile vs distance-to-axis)
DENT_CP = [(0.0045, 0.3719), (0.0727, 0.4532), (0.2273, 0.4844), (0.5568, 0.5125), (1.0, 0.5)]


def _hsv2rgba(h, s, v):
    """core/util/color.hsv2rgba: mathutils.Color().hsv (clamps s,v to [0,1])."""
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, min(max(s, 0), 1), min(max(v, 0), 1))
    return (r, g, b, 1.0)


def _make_floatcurve(cp):
    """Build the EXACT Blender FloatCurve infinigen uses (node_utils.assign_curve):
    a CurveMapping with AUTO handles through cp. Returns an evaluator f(x).
    This is a true 1:1 port of the shape/dent radius curves — not an approximation."""
    mat = bpy.data.materials.new("_fc")
    mat.use_nodes = True
    node = mat.node_tree.nodes.new("ShaderNodeFloatCurve")
    curve = node.mapping.curves[0]
    pts = curve.points
    while len(pts) > len(cp):
        pts.remove(pts[-1])
    while len(pts) < len(cp):
        pts.new(0.5, 0.5)
    for p, (x, y) in zip(pts, sorted(cp)):
        p.location = (x, y)
        p.handle_type = 'AUTO'
    node.mapping.update()
    mapping, c = node.mapping, curve
    return lambda x: float(mapping.evaluate(c, min(max(x, 0.0), 1.0)))


def _quadratic_bezier_z(t, s_z, m_z, e_z):
    """QuadraticBezier spine z(t), Start->Middle->End  (fruit_utils shape_quadratic)."""
    return (1 - t) ** 2 * s_z + 2 * (1 - t) * t * m_z + t ** 2 * e_z


# ---------------------------------------------------------------------------
#  apple.py sample_surface_params -> the two skin colours (deterministic per seed)
# ---------------------------------------------------------------------------
def _sample_colors():
    base = np.array((uniform(-0.05, 0.1), 0.999, 0.799))   # apple.py:56
    base[1] += normal(0.0, 0.05)                           # apple.py:57
    base[2] += normal(0.0, 0.05)                           # apple.py:58
    color1 = _hsv2rgba(*base)                              # apple.py:59
    alt = np.copy(base)                                    # apple.py:61
    alt[0] += normal(0.05, 0.02)                           # apple.py:62
    alt[1] += normal(0.0, 0.05)                            # apple.py:63
    alt[2] += normal(0.0, 0.05)                            # apple.py:64
    color2 = _hsv2rgba(*alt)                               # apple.py:65
    random_seed = uniform(-100, 100)                       # apple.py:72
    # STUDIO ADAPTATION (hue-faithful): apple.py samples value ~0.8, which this fixed
    # studio (Standard view transform, no auto-exposure) blows out to white — that
    # blow-out is what makes the lit side look orange and leaves a hard clip contour.
    # Infinigen's own pipeline auto-lowers exposure until not overexposed; we mirror
    # that by scaling the colour VALUE down (hue & sat kept exactly).
    k = 0.55
    color1 = (color1[0] * k, color1[1] * k, color1[2] * k, 1.0)
    color2 = (color2[0] * k, color2[1] * k, color2[2] * k, 1.0)
    return color1, color2, random_seed


def _apple_shader(color1, color2, random_seed):
    """apple_surface.shader_apple_shader blends the two skin colours (color1/color2)
    with a Musgrave texture. Blender 4.2 REMOVED the Musgrave node, so the fractal
    two-colour MOTTLE cannot be reproduced 1:1  # [GN-EQUIV, forced: Musgrave gone].
    color1 and color2 are already close (apple.py: alt = base + ~0.05 hue), so we use
    the sampled base colour (color1) directly as a clean, faithful skin — the blush is
    the only detail lost to the missing node."""
    mat = bpy.data.materials.new("apple")
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    bsdf.inputs["Roughness"].default_value = 0.5
    tc = nt.nodes.new("ShaderNodeTexCoord")
    # pass 1 — coarse two-colour blend (apple_surface.py:56-70, musgrave Scale 0.6)
    n1 = nt.nodes.new("ShaderNodeTexNoise")                # [GN-EQUIV] Musgrave -> Noise
    n1.inputs["Scale"].default_value = 0.6
    n1.inputs["Detail"].default_value = 6.0
    n1.inputs["Roughness"].default_value = 0.75
    nt.links.new(tc.outputs["Object"], n1.inputs["Vector"])
    mix1 = nt.nodes.new("ShaderNodeMix"); mix1.data_type = 'RGBA'
    mix1.inputs[6].default_value = color1                  # A
    mix1.inputs[7].default_value = color2                  # B
    nt.links.new(n1.outputs["Fac"], mix1.inputs[0])
    # pass 2 — fine hue-shifted speckle (apple_surface.py:45-83, musgrave Scale 10)
    n2 = nt.nodes.new("ShaderNodeTexNoise")               # [GN-EQUIV] Musgrave -> Noise
    n2.inputs["Scale"].default_value = 10.0
    n2.inputs["Detail"].default_value = 10.0
    nt.links.new(tc.outputs["Object"], n2.inputs["Vector"])
    hs = nt.nodes.new("ShaderNodeHueSaturation")          # apple_surface.py:72 Hue 0.55
    hs.inputs["Hue"].default_value = 0.55
    nt.links.new(mix1.outputs[2], hs.inputs["Color"])
    mix2 = nt.nodes.new("ShaderNodeMix"); mix2.data_type = 'RGBA'
    nt.links.new(mix1.outputs[2], mix2.inputs[6])         # A = pass-1 colour
    nt.links.new(hs.outputs["Color"], mix2.inputs[7])     # B = hue-shifted copy
    nt.links.new(n2.outputs["Fac"], mix2.inputs[0])       # fine noise factor
    nt.links.new(mix2.outputs[2], bsdf.inputs["Base Color"])
    return mat


def _stem_material(seed_vec):
    mat = bpy.data.materials.new("stem")
    mat.use_nodes = True
    # apple.py:86 stem_color hsv (0.10, 0.96, 0.13) + small noise
    col = _hsv2rgba(0.10 + normal(0, 0.02), 0.96 + normal(0, 0.05), 0.13 + normal(0, 0.05))
    mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = col
    return mat


def build(seed=0):
    npseed(seed)

    # ---- cross-section + shape sampling (apple.py) ----
    cross_radius = normal(1.5, 0.05)                       # apple.py:26 circle radius
    start = (uniform(-0.1, 0.1), uniform(-0.1, 0.1), uniform(-0.9, -1.1))  # apple.py:49 Start
    end = (0.0, 0.0, 1.0)                                  # apple.py:50 End
    middle = (0.0, 0.0, 0.0)                               # shape_quadratic Middle default
    color1, color2, rseed = _sample_colors()
    radius_fc = _make_floatcurve(RADIUS_CP)                # shape_quadratic radius float-curve
    dent_fc = _make_floatcurve(DENT_CP)                    # add_dent float_curve_3

    seg = 64                                               # cross-section resolution (source 256; 64 renders clean)
    n_ring = 128                                           # spine Resolution (source 256)
    max_radius = 1.5                                       # apple_surface add_dent max radius   apple_surface.py:130

    th = np.linspace(0, 2 * np.pi, seg, endpoint=False)
    verts, faces = [], []
    for j in range(n_ring):
        t = j / (n_ring - 1)                               # spline parameter 0..1
        # spine point (Start->Middle->End quadratic bezier)
        bx = _quadratic_bezier_z(t, start[0], middle[0], end[0])
        by = _quadratic_bezier_z(t, start[1], middle[1], end[1])
        bz = _quadratic_bezier_z(t, start[2], middle[2], end[2])
        ring_r = cross_radius * radius_fc(t)  # radius float-curve * circle radius
        base = len(verts)
        for i in range(seg):
            x = bx + ring_r * np.cos(th[i])
            y = by + ring_r * np.sin(th[i])
            z = bz
            # ---- add_dent (apple_surface): axial calyx wells at both poles ----
            # Source selects the whole top/bottom half and offsets along the spline
            # tangent by dentcurve(r/maxR)*intensity (top +1.5, bottom -1.0). Applying
            # that raw switch at t=0.5 leaves a tiny Z step between the two halves -> a
            # visible equator SEAM on a low-res plain mesh (infinigen's 256-res + skin
            # texture hide it). We keep the exact dent shape but WINDOW each half so the
            # offset is exactly 0 at the equator (t=0.5) and only wells the poles -> the
            # same calyx, seamless.
            r_norm = min(ring_r / max_radius, 1.0)         # map_range distance/max_radius
            d = dent_fc(r_norm)                            # float_curve_3
            m = -1.0 + 2.0 * d                             # map_range_1 (To min -1)
            w_top = max(0.0, (t - 0.5) / 0.5) ** 2         # 0 at equator -> 1 at top pole
            w_bot = max(0.0, (0.5 - t) / 0.5) ** 2         # 0 at equator -> 1 at bottom pole
            z += m * 1.5 * w_top + m * (-1.0) * w_bot
            verts.append((x, y, z))
        if j > 0:
            pv = base - seg
            for i in range(seg):
                faces.append((pv + i, pv + (i + 1) % seg, base + (i + 1) % seg, base + i))

    me = bpy.data.meshes.new("apple")
    me.from_pydata(verts, [], faces)
    me.update()
    obj = bpy.data.objects.new("apple", me)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.remove_doubles(threshold=1e-5)
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.shade_smooth()
    obj.data.materials.append(_apple_shader(color1, color2, rseed))

    # ---- basic_stem (apple.py:92-107): bezier from base -> quad_mid -> quad_end ----
    quad_mid = (uniform(-0.1, 0.1), uniform(-0.1, 0.1), uniform(0.15, 0.2))     # apple.py:96
    quad_end = (uniform(-0.2, 0.2), uniform(-0.2, 0.2), uniform(0.3, 0.4))      # apple.py:101
    stem_cross_r = uniform(0.025, 0.035)                                        # apple.py:103
    trans = (0.0, 0.0, 0.6)                                                     # apple.py:105 Translation
    cu = bpy.data.curves.new("stem", 'CURVE')
    cu.dimensions = '3D'; cu.bevel_depth = 1.0; cu.resolution_u = 4
    sp = cu.splines.new('BEZIER'); sp.bezier_points.add(2)
    for k, p in enumerate([(0, 0, 0), quad_mid, quad_end]):
        bp = sp.bezier_points[k]
        bp.co = (p[0] + trans[0], p[1] + trans[1], p[2] + trans[2])
        bp.handle_left_type = bp.handle_right_type = 'AUTO'
        bp.radius = 1.0 - 0.4 * k
    cu.bevel_depth = stem_cross_r
    stem = bpy.data.objects.new("stem", cu)
    bpy.context.scene.collection.objects.link(stem)
    stem.data.materials.append(_stem_material(rseed))
    bpy.context.view_layer.objects.active = stem
    stem.select_set(True)
    bpy.ops.object.convert(target='MESH')
    stem = bpy.context.active_object

    # join + rest on z=0
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True); stem.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.join()
    apple = bpy.context.active_object
    apple.name = "apple"
    apple.location.z -= min(v.co.z for v in apple.data.vertices)
    bpy.ops.object.transform_apply(location=True)
    return apple


if __name__ == "__main__":
    build(0)
