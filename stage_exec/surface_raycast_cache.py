"""Process-local evaluated surface cache shared by placement and machine review."""

from __future__ import annotations

from mathutils import Vector


_SCENE_KEY = None
_ENTRIES = None


def _is_placeholder(obj) -> bool:
    return any(coll.name.startswith("placeholders:") for coll in obj.users_collection)


def entries(scene):
    """Return render-visible non-placeholder evaluated meshes for this open scene."""
    global _SCENE_KEY, _ENTRIES
    key = scene.as_pointer()
    if _SCENE_KEY == key and _ENTRIES is not None:
        return _ENTRIES
    deps = __import__("bpy").context.evaluated_depsgraph_get()
    built = []
    for obj in scene.objects:
        if (obj.type != "MESH" or obj.hide_render or _is_placeholder(obj)
                or "collider" in obj.name.lower()):
            continue
        ev = obj.evaluated_get(deps)
        mesh = getattr(ev, "data", None)
        if mesh is None or not hasattr(mesh, "vertices") or len(mesh.vertices) == 0:
            continue
        matrix = ev.matrix_world.copy()
        try:
            inverse = matrix.inverted()
        except (AttributeError, RuntimeError, ValueError):
            continue
        # Some Geometry Nodes carriers and asset-library masters expose a
        # non-empty ``data`` mesh but have no evaluated mesh that Object.ray_cast
        # can consume.  Without this one-time probe Blender emits the same
        # "has no evaluated mesh data" error for every planning-grid ray and
        # footprint corner.  A valid evaluated mesh accepts the call even when
        # this deliberately tiny local-space ray misses it.
        try:
            ev.ray_cast(
                Vector((0.0, 0.0, 0.0)),
                Vector((0.0, 0.0, 1.0)),
                distance=1e-6,
            )
        except (AttributeError, RuntimeError, ValueError, ReferenceError):
            continue
        built.append((obj.name, ev, matrix, inverse))
    _SCENE_KEY, _ENTRIES = key, built
    print(f"[surface-cache] built {len(built)} evaluated meshes", flush=True)
    return built


def raycast(scene, origin, direction, *, distance=1e5):
    """Return nearest hit as (distance, location, object_name, world_normal)."""
    origin, direction = Vector(origin), Vector(direction).normalized()
    best = None
    for object_name, ev, matrix, inverse in entries(scene):
        try:
            ok, loc_l, normal_l, _index = ev.ray_cast(
                inverse @ origin,
                (inverse.to_3x3() @ direction).normalized(),
                distance=float(distance),
            )
        except (AttributeError, RuntimeError, ValueError, ReferenceError):
            continue
        if not ok:
            continue
        loc_w = matrix @ loc_l
        hit_distance = (loc_w - origin).length
        if hit_distance > float(distance) or (best is not None and hit_distance >= best[0]):
            continue
        normal_w = (
            matrix.to_3x3().inverted().transposed() @ normal_l
        ).normalized()
        best = (hit_distance, loc_w, object_name, normal_w)
    return best


def clear() -> None:
    global _SCENE_KEY, _ENTRIES
    _SCENE_KEY, _ENTRIES = None, None
