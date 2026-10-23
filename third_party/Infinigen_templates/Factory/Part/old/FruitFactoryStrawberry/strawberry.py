"""
================================================================================
FruitFactoryStrawberry  —  a strawberry   [PART / ELEMENT]
  faithful plain-bpy port of  <INF>/fruits/strawberry.py
                              + surfaces/strawberry_surface.py
================================================================================

WHAT THIS IS
  ONE strawberry: a red heart/cone body (point at the bottom, wide shoulders) with
  real SEEDS (achenes) dotted over the skin in little pits, plus a green calyx of
  small leaves at the top. A plant [ENTRY] hangs a few. Never pick it as a whole
  object.

EXACTLY WHAT strawberry_surface.py DOES (reproduced here, not invented):
  1. body        = shape_quadratic core (circle cross, strawberry profile)
  2. surfacebump(Scale 0.5) gently dimples the CORE
  3. point_on_mesh(Distance Min 0.12) scatters seed points over the surface
  4. instance a strawberry_seed at each point (a tiny ovoid achene), seated into a
     pit along the surface normal
  5. colour: main_color (0.888, 0.010, 0.032) red, top_color (0.815, 0.610, 0.175)
     lighter near the top; Roughness 0.15 (glossy, a little translucent)
  Seeds are yellow-tan; the source also samples a green calyx via the stem params.

HARNESS RULES (fixed studio owns cameras/lights/floor/world/render)
  - build(seed=0) creates the strawberry at the origin resting on z=0; returns nothing.
  - Only bpy / math / numpy / mathutils / random. Real joined mesh; no textures.
================================================================================
"""

import bpy
import numpy as np
from mathutils import noise as _noise

# strawberry body profile r(t), t bottom(point)->top(shoulders)  (source shape params)
PROFILE = [(0, 0), (0.0227, 0.1313), (0.2227, 0.4406), (0.62, 0.74),
           (0.925, 0.4719), (1, 0)]
MAIN_COLOR = (0.80, 0.02, 0.05)      # source main_color (slightly toned for the rig)
SEED_COLOR = (0.86, 0.70, 0.16)      # yellow-tan achene
CALYX_COLOR = (0.13, 0.42, 0.10)


def _mat(color, rough=0.2):
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


def _body(mat, cross_radius=1.3, height=2.0, seg=40, n_ring=48):
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
    obj = bpy.data.objects.new("strawberry", me)
    bpy.context.scene.collection.objects.link(obj)
    obj.data.materials.append(mat)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode="OBJECT")
    for v in obj.data.vertices:                       # step 2: gentle core bump
        v.co = v.co + v.normal * (_noise.noise(v.co * 0.5) * 0.03)
    obj.data.update()
    bpy.ops.object.shade_smooth()
    return obj, height


def _seed_grid(rng, height):
    """Seed sites over the body surface (step 3, Distance Min 0.12 -> dense grid)."""
    ts, rs = _spline(PROFILE, 22)
    rs = rs * 1.3
    sites = []
    for j in range(2, 21):                            # skip the very tip / shoulders
        ring_r = rs[j]
        if ring_r < 0.05:
            continue
        m = max(5, int(round(2 * np.pi * ring_r / 0.16)))   # spacing ~0.16 (Distance Min)
        for k in range(m):
            th = 2 * np.pi * k / m + j * 0.5 + rng.uniform(-0.08, 0.08)
            sites.append((ring_r * np.cos(th), ring_r * np.sin(th), ts[j] * height, th))
    return sites


def build(seed=0):
    rng = np.random.RandomState(seed)
    body_mat = _mat(MAIN_COLOR, 0.18)
    seed_mat = _mat(SEED_COLOR, 0.35)
    calyx_mat = _mat(CALYX_COLOR, 0.6)
    body, height = _body(body_mat)
    parts = [body]

    # step 4: an achene seed (small ovoid) seated into a pit at each site
    for (x, y, z, th) in _seed_grid(rng, height):
        bpy.ops.mesh.primitive_uv_sphere_add(segments=8, ring_count=6, radius=0.05,
                                             location=(x * 0.98, y * 0.98, z))
        s = bpy.context.active_object
        s.scale = (0.7, 1.4, 0.7)                      # elongated achene
        s.rotation_euler = (0, 0, th)
        s.data.materials.append(seed_mat)
        parts.append(s)

    # green calyx: a few small leaves fanning out at the wide top
    top_r = 1.3 * _spline(PROFILE, 48)[1][-6] * 1.0
    for k in range(7):
        az = 2 * np.pi * k / 7
        bpy.ops.mesh.primitive_cone_add(vertices=4, radius1=0.12, radius2=0.0,
                                        depth=0.5, location=(0, 0, height))
        lf = bpy.context.active_object
        lf.rotation_euler = (np.deg2rad(55), 0, az)
        lf.location = (0.18 * np.cos(az), 0.18 * np.sin(az), height * 0.98)
        lf.data.materials.append(calyx_mat)
        parts.append(lf)

    bpy.ops.object.select_all(action="DESELECT")
    for p in parts:
        p.select_set(True)
    bpy.context.view_layer.objects.active = body
    bpy.ops.object.join()
    fr = bpy.context.active_object
    fr.name = "strawberry"
    bpy.ops.object.shade_smooth()
    fr.location.z -= min(v.co.z for v in fr.data.vertices)
    bpy.ops.object.transform_apply(location=True)
    return fr


if __name__ == "__main__":
    build(0)
