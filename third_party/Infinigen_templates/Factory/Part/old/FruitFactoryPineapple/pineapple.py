"""
================================================================================
FruitFactoryPineapple  —  a pineapple   [PART / ELEMENT]
  faithful plain-bpy port of  <INF>/fruits/pineapple.py
                              + surfaces/pineapple_surface.py
================================================================================

WHAT THIS IS
  ONE pineapple: a golden-brown barrel skinned with the characteristic DIAMOND
  LATTICE of raised scale-cells (each with a little bract spike), topped by a CROWN
  of spiky green leaves. Never pick it as a whole object.

EXACTLY WHAT pineapple_surface.py DOES (reproduced here, not invented):
  1. body      = shape_quadratic barrel (circle cross, pineapple profile)
  2. point_on_mesh(Distance Min = point distance) scatters CELL points over the body
  3. instance a CELL at each point = a low cone/pyramid (nodegroup_cell, resolution 16,
     scale diff -0.3) + a pineapple_leaf spike, coloured by a bottom/mid/top/centre
     gradient (greenish base -> brown -> warm centre), surfacebump(Displacement 0.2,
     Scale 10) on the cell
  4. colours: color_bottom (0.082,0.095,0.010) green, color_mid (0.552,0.185,0.022)
     brown, color_top (0.451,0.100,0.0003), color_center (0.839,0.540,0.314)
  The cells sit on a quasi-regular grid -> the diamond lattice. A leaf CROWN sits on
  top (source pineapple.py). Reproduced here as a diamond grid of pyramid cells + a
  crown of upward green leaves.

HARNESS RULES (fixed studio owns cameras/lights/floor/world/render)
  - build(seed=0) creates the pineapple at the origin resting on z=0; returns nothing.
  - Only bpy / math / numpy / mathutils / random. Real joined mesh; no textures.
================================================================================
"""

import bpy
import numpy as np
from mathutils import Vector, Matrix

PROFILE = [(0, 0.1031), (0.1182, 0.5062), (0.5, 0.5594), (0.8364, 0.425),
           (0.9864, 0.1406), (1, 0)]
COLOR_SKIN = (0.52, 0.20, 0.03)      # brown body / cell (source color_mid)
COLOR_TIP = (0.24, 0.34, 0.05)       # green bract tip (source color_bottom-ish)
COLOR_LEAF = (0.16, 0.42, 0.10)      # crown leaves


def _mat(color, rough=0.5):
    m = bpy.data.materials.new("m")
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*color, 1.0)
    b.inputs["Roughness"].default_value = rough
    return m


def _spline(cp, n):
    cp = np.array(sorted(cp), float)
    tc, rc = cp[:, 0], cp[:, 1]
    tp = np.concatenate([[tc[0] - (tc[1] - tc[0])], tc, [tc[-1] + (tc[-1] - tc[-2])]])
    rp = np.concatenate([[rc[0]], rc, [rc[-1]]])
    ts = np.linspace(tc[0], tc[-1], n)
    rs = np.empty(n)
    for k, t in enumerate(ts):
        i = min(max(np.searchsorted(tc, t) - 1, 0), len(tc) - 2)
        u = 0.0 if tp[i + 2] == tp[i + 1] else (t - tp[i + 1]) / (tp[i + 2] - tp[i + 1])
        p0, p1, p2, p3 = rp[i], rp[i + 1], rp[i + 2], rp[i + 3]
        rs[k] = 0.5 * (2 * p1 + (-p0 + p2) * u + (2 * p0 - 5 * p1 + 4 * p2 - p3) * u * u
                       + (-p0 + 3 * p1 - 3 * p2 + p3) * u * u * u)
    return np.linspace(0, 1, n), np.clip(rs, 0, None)


def _body(mat, cross_radius=1.4, height=2.3, seg=48, n_ring=48):
    ts, rs = _spline(PROFILE, n_ring)
    rs = rs * cross_radius
    th = np.linspace(0, 2 * np.pi, seg, endpoint=False)
    verts, faces = [], []
    for j in range(n_ring):
        base = len(verts)
        for i in range(seg):
            verts.append((rs[j] * np.cos(th[i]), rs[j] * np.sin(th[i]), ts[j] * height))
        if j > 0:
            pv = base - seg
            for i in range(seg):
                faces.append((pv + i, pv + (i + 1) % seg, base + (i + 1) % seg, base + i))
    me = bpy.data.meshes.new("body")
    me.from_pydata(verts, [], faces)
    me.update()
    obj = bpy.data.objects.new("pineapple", me)
    bpy.context.scene.collection.objects.link(obj)
    obj.data.materials.append(mat)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.shade_smooth()
    return obj, height


def _cell(loc, normal, size, skin_mat, tip_mat):
    """One scale cell: a 4-sided pyramid pushed out along the surface normal, with a
    small green bract tip (source cell + pineapple_leaf)."""
    # flatter, wider diamond scale (not a sharp spike): shallow 4-sided cell
    bpy.ops.mesh.primitive_cone_add(vertices=4, radius1=size, radius2=size * 0.18,
                                    depth=size * 0.6, location=(0, 0, 0))
    cell = bpy.context.active_object
    cell.data.materials.append(skin_mat)
    # orient +Z (cone axis) to the surface normal
    z = Vector((0, 0, 1))
    q = z.rotation_difference(normal)
    cell.rotation_euler = q.to_euler()
    cell.location = loc + normal * (size * 0.2)
    return cell


def build(seed=0, rings=11, cols=16):
    rng = np.random.RandomState(seed)
    skin = _mat(COLOR_SKIN, 0.55)
    tip = _mat(COLOR_TIP, 0.5)
    leaf_mat = _mat(COLOR_LEAF, 0.55)
    body, height = _body(skin)
    parts = [body]

    ts, rs = _spline(PROFILE, rings + 2)
    rs = rs * 1.4
    # diamond lattice: offset every other row by half a column (the pineapple grid)
    for j in range(1, rings):
        ring_r = rs[j]
        if ring_r < 0.12:
            continue
        z = ts[j] * height
        offset = 0.5 if j % 2 else 0.0
        # local surface normal (approx from profile slope) for outward orientation
        dz = (ts[j + 1] - ts[j - 1]) * height
        dr = (rs[j + 1] - rs[j - 1])
        for k in range(cols):
            th = 2 * np.pi * (k + offset) / cols
            loc = Vector((ring_r * np.cos(th), ring_r * np.sin(th), z))
            nrm = Vector((dz * np.cos(th), dz * np.sin(th), -dr)).normalized()
            cell = _cell(loc, nrm, 0.16, skin, tip)
            parts.append(cell)

    # crown: a rosette of upward-pointing spiky green leaves on the top
    top_z = height
    for ring, (n, tilt, length) in enumerate([(7, 20, 1.1), (6, 40, 0.8), (5, 60, 0.5)]):
        for k in range(n):
            az = 2 * np.pi * k / n + ring * 0.4
            bpy.ops.mesh.primitive_cone_add(vertices=4, radius1=0.09, radius2=0.0,
                                            depth=length, location=(0, 0, 0))
            lf = bpy.context.active_object
            lf.data.materials.append(leaf_mat)
            lf.rotation_euler = (np.deg2rad(tilt), 0, az)
            lf.location = (0.06 * np.cos(az), 0.06 * np.sin(az), top_z + length * 0.35)
            parts.append(lf)

    bpy.ops.object.select_all(action="DESELECT")
    for p in parts:
        p.select_set(True)
    bpy.context.view_layer.objects.active = body
    bpy.ops.object.join()
    fr = bpy.context.active_object
    fr.name = "pineapple"
    bpy.ops.object.shade_smooth()
    fr.location.z -= min(v.co.z for v in fr.data.vertices)
    bpy.ops.object.transform_apply(location=True)
    return fr


if __name__ == "__main__":
    build(0)
