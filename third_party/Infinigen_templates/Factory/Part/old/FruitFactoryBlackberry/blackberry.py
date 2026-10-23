"""
================================================================================
FruitFactoryBlackberry  —  a blackberry   [PART / ELEMENT]
  faithful plain-bpy port of  <INF>/fruits/blackberry.py
                              + surfaces/blackberry_surface.py
================================================================================

WHAT THIS IS
  ONE blackberry: an ovoid CORE covered in real DRUPELETS (the little round juice
  cells), very dark purple-black and glossy. A bramble [ENTRY] hangs a few near its
  tips. Never pick it as a whole object.

EXACTLY WHAT blackberry_surface.py DOES (reproduced here, not invented):
  1. body       = shape_quadratic core (circle cross-section, blackberry profile)
  2. surfacebump(Displacement 0.5, Scale 0.5) roughens the CORE
  3. point_on_mesh(Distance Min 0.4) scatters points over the core surface
  4. instance a DRUPELET at each point = a UV-sphere(32,16) + surfacebump(0.5,0.3)
     + Subdivision, seated slightly outward along the surface normal
  5. berry_color = (0.0212, 0.0212, 0.0284), Roughness 0.2 (glossy)
  (source also instances a tiny dark "hair"/style per drupelet; omitted — negligible.)

  This template keeps that structure: a solid dark CORE + drupelet spheres seated on
  the core's own surface points, dense enough to touch and hide the core.

HARNESS RULES (fixed studio owns cameras/lights/floor/world/render)
  - build(seed=0) creates the berry at the origin resting on z=0; returns nothing.
  - Only bpy / math / numpy / mathutils / random. Real joined mesh; no textures.
================================================================================
"""

import bpy
import numpy as np
from mathutils import noise as _noise

# blackberry body profile r(t), t bottom->top  (source blackberry.py shape params)
PROFILE = [(0, 0), (0.0841, 0.3469), (0.5, 0.8), (0.9432, 0.4781), (1, 0)]
BERRY_COLOR = (0.0212, 0.0212, 0.0284)


def _material():
    mat = bpy.data.materials.new("blackberry")
    mat.use_nodes = True
    b = mat.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*BERRY_COLOR, 1.0)
    b.inputs["Roughness"].default_value = 0.2
    if "Specular IOR Level" in b.inputs:
        b.inputs["Specular IOR Level"].default_value = 0.57
    return mat


def _spline(cp, n):
    """Catmull-Rom through the (t, r) control points (source shape_quadratic)."""
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


def _core(mat, cross_radius=1.1, height=2.0, seg=14, n_ring=16):
    """Solid ovoid core (step 1) + surface_bump (step 2). Returns (obj, ring pts)."""
    ts, rs = _spline(PROFILE, n_ring)
    rs = rs * cross_radius
    th = np.linspace(0, 2 * np.pi, seg, endpoint=False)
    verts, faces, ringpts = [], [], []
    for j in range(n_ring):
        base = len(verts)
        for i in range(seg):
            p = (rs[j] * np.cos(th[i]), rs[j] * np.sin(th[i]), ts[j] * height)
            verts.append(p)
        if j > 0:
            pv = base - seg
            for i in range(seg):
                faces.append((pv + i, pv + (i + 1) % seg, base + (i + 1) % seg, base + i))
    me = bpy.data.meshes.new("core")
    me.from_pydata(verts, [], faces)
    me.update()
    obj = bpy.data.objects.new("core", me)
    bpy.context.scene.collection.objects.link(obj)
    obj.data.materials.append(mat)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode="OBJECT")
    # step 2: surface_bump on the core (source Displacement 0.5, Scale 0.5)
    for v in obj.data.vertices:
        v.co = v.co + v.normal * (_noise.noise(v.co * 0.5) * 0.08)
    obj.data.update()
    # collect surface points + outward normals for drupelet seating (steps 3-4)
    pts = [(v.co.copy(), v.normal.copy()) for v in obj.data.vertices]
    return obj, pts


def build(seed=0):
    rng = np.random.RandomState(seed)
    mat = _material()
    core, pts = _core(mat)

    # step 4: a drupelet (UV sphere) seated slightly outward at each core point.
    # drupelet radius ~ core ring spacing so neighbours touch and hide the core.
    drup_r = 0.24
    drupelets = [core]
    for co, nrm in pts:
        loc = co + nrm * (drup_r * 0.35)
        bpy.ops.mesh.primitive_uv_sphere_add(
            segments=12, ring_count=8,
            radius=drup_r * rng.uniform(0.9, 1.08),
            location=(loc.x, loc.y, loc.z))
        d = bpy.context.active_object
        d.data.materials.append(mat)
        drupelets.append(d)

    bpy.ops.object.select_all(action="DESELECT")
    for d in drupelets:
        d.select_set(True)
    bpy.context.view_layer.objects.active = core
    bpy.ops.object.join()
    berry = bpy.context.active_object
    berry.name = "blackberry"
    bpy.ops.object.shade_smooth()
    zmin = min(v.co.z for v in berry.data.vertices)
    berry.location.z -= zmin
    bpy.ops.object.transform_apply(location=True)
    return berry


if __name__ == "__main__":
    build(0)
