"""Camera-visible scatter constraints and post-creation visibility metrics."""
from __future__ import annotations

import math
import random
from contextlib import contextmanager

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Vector

import surface_registry as sr
import surface_raycast_cache as raycache


def _sample_axis(count: int, margin: float) -> tuple[float, ...]:
    if count <= 1:
        return (0.5,)
    step = (1.0 - margin * 2.0) / float(count - 1)
    return tuple(margin + index * step for index in range(count))


def camera_basis(camera):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    matrix = camera.evaluated_get(depsgraph).matrix_world.copy()
    origin = matrix.translation.copy()
    forward = -Vector((matrix[0][2], matrix[1][2], matrix[2][2])).normalized()
    right = Vector((matrix[0][0], matrix[1][0], matrix[2][0])).normalized()
    up = Vector((matrix[0][1], matrix[1][1], matrix[2][1])).normalized()
    return origin, forward, right, up


def collect_visible_surface_anchors(
        scene, camera, scene_name: str, required_surface_types,
        *, columns: int = 9, rows: int = 6, margin: float = 0.07) -> list[dict]:
    """Ray-sample nearest visible semantic terrain and retain its world hits."""
    required = {str(value).strip().lower()
                for value in (required_surface_types or []) if str(value).strip()}
    origin, forward, right, up = camera_basis(camera)
    tan_x = math.tan(camera.data.angle_x / 2.0)
    tan_y = math.tan(camera.data.angle_y / 2.0)
    anchors = []
    for nx in _sample_axis(columns, margin):
        for ny in _sample_axis(rows, margin):
            direction = (
                right * ((nx * 2.0 - 1.0) * tan_x)
                + up * ((ny * 2.0 - 1.0) * tan_y)
                + forward
            ).normalized()
            hit = raycache.raycast(scene, origin, direction)
            if hit is None:
                continue
            distance, location, object_name, normal = hit
            surface_types = set(sr.types_for_mesh(scene_name, object_name))
            semantic_evidence = "mesh_level_registry"
            if (
                not surface_types
                and required
                and object_name.casefold().startswith(
                    ("opaqueterrain", "collectivetransparentterrain")
                )
            ):
                # Baked e2e paths do not always contain the original scene name.
                # The director already validated these requested types; here the
                # ray proves only that their shared terrain carrier is visible.
                surface_types = set(required)
                semantic_evidence = "director_policy_on_visible_terrain_mesh"
            if not surface_types:
                continue
            if required and not required.intersection(surface_types):
                continue
            # Cover adjacent rays without extending to the whole terrain. Radius
            # follows angular sample spacing and is capped to avoid a distant
            # anchor becoming a giant hard-edged patch.
            angular_step = min(2.0 * tan_x / columns, 2.0 * tan_y / rows)
            radius = max(1.25, min(6.0, float(distance) * angular_step * 1.35))
            anchors.append({
                "screen": [round(nx, 5), round(ny, 5)],
                "world": [float(value) for value in location],
                "normal": [float(value) for value in normal],
                "distance": float(distance),
                "radius": radius,
                "object": object_name,
                "surface_types": sorted(surface_types),
                "semantic_evidence": semantic_evidence,
            })
    return anchors


def anchor_reach(base_obj, anchors) -> dict:
    """How much of the camera-visible band this scatter base can actually carry.

    The official per-system terrain target is an LOD chunk, not the whole
    terrain: forest/seed0's ``inview_inview`` stops at 43.0m while the camera
    still sees ground out to ~74m.  Scattering onto it therefore cuts the cover
    off at a hard chunk edge and silently throws away every anchor beyond it.
    """
    from mathutils import kdtree

    matrix = base_obj.matrix_world
    vertices = base_obj.data.vertices
    tree = kdtree.KDTree(len(vertices))
    for index, vertex in enumerate(vertices):
        tree.insert(matrix @ vertex.co, index)
    tree.balance()

    reached = 0
    for row in anchors:
        point = Vector(row["world"])
        radius = float(row["radius"])
        _co, _index, distance = tree.find(point)
        if distance is not None and distance <= radius:
            reached += 1
    total = max(1, len(anchors))
    return {
        "object": base_obj.name,
        "vertices": len(vertices),
        "anchors_reached": reached,
        "anchors_total": len(anchors),
        "reach_fraction": round(reached / total, 4),
    }


def pick_base_covering_anchors(candidates, anchors, *, min_fraction=0.9):
    """Keep the official base when it reaches the visible band, else widen.

    ``candidates`` is ordered official-first.  The first entry that reaches at
    least ``min_fraction`` of the anchors wins; otherwise the entry with the
    best reach wins, so widening is a measured escalation and never a guess.
    """
    measured = []
    for obj in candidates:
        if obj is None or obj.type != "MESH":
            continue
        measured.append((obj, anchor_reach(obj, anchors)))
    if not measured:
        return None, []
    for obj, reach in measured:
        if reach["reach_fraction"] >= min_fraction:
            return obj, [row for _obj, row in measured]
    best = max(measured, key=lambda item: item[1]["reach_fraction"])
    return best[0], [row for _obj, row in measured]


def raise_taper_floor(base_selection, floor: float):
    """Stop the official edge taper from shrinking new cover to nothing.

    ``placement_mask(return_scalar=True)`` returns a second value that is a
    SMOOTHSTEP MapRange of the same noise from ``select_thresh`` up to 0.75.
    ``scatter_instances`` multiplies either instance scale (``taper_scale``:
    flowerplant, grass, ground_mushroom) or density (``taper_density``: pebbles,
    pine_needle, seashells, ground_leaves, ground_twigs, decorative_plants,
    pinecone, mollusk) by it, so the scalar reaches 0 exactly at the mask edge.

    That interacts badly with lowering ``select_thresh`` for camera coverage:
    dropping the threshold widens the ramp's lower bound, so every square metre
    the drop wins back sits in the part of the ramp where the scalar is ~0.
    0909 flowers is that failure exactly — 0 instances became 3465 shrunken
    ones.  Remapping the scalar to [floor, 1] keeps the official fade in place
    but stops it from bottoming out.
    """
    floor = max(0.0, min(1.0, float(floor)))

    def selection(node_wrangler):
        from infinigen.core import surface

        evaluated = surface.eval_argument(node_wrangler, base_selection)
        if not isinstance(evaluated, tuple):
            return evaluated
        mask, taper = evaluated[0], evaluated[1]
        lifted = node_wrangler.scalar_add(
            node_wrangler.scalar_multiply(taper, 1.0 - floor), floor
        )
        return mask, lifted

    return selection


def restrict_to_visible_anchors(base_selection, anchors):
    """Intersect an Infinigen selection with a union of visible hit patches."""
    frozen = tuple(
        (tuple(float(value) for value in row["world"]), float(row["radius"]))
        for row in anchors
    )

    def selection(node_wrangler):
        from infinigen.core import surface
        from infinigen.core.nodes.node_info import Nodes

        evaluated = surface.eval_argument(node_wrangler, base_selection)
        taper = None
        if isinstance(evaluated, tuple):
            mask, taper = evaluated[0], evaluated[1]
        else:
            mask = evaluated
        position = node_wrangler.new_node(Nodes.InputPosition)
        inside = []
        for point, radius in frozen:
            distance = node_wrangler.new_node(
                Nodes.VectorMath,
                input_kwargs={0: position, 1: point},
                attrs={"operation": "DISTANCE"},
            )
            inside.append(node_wrangler.new_node(
                Nodes.Compare,
                attrs={"operation": "LESS_THAN", "data_type": "FLOAT"},
                input_args=[distance.outputs["Value"], radius],
            ))
        if not inside:
            visible_mask = 0.0
        elif len(inside) == 1:
            visible_mask = inside[0]
        else:
            summed = node_wrangler.scalar_add(*inside)
            visible_mask = node_wrangler.new_node(
                Nodes.Compare,
                attrs={"operation": "GREATER_THAN", "data_type": "FLOAT"},
                input_args=[summed, 0.5],
            )
        mask = node_wrangler.scalar_multiply(mask, visible_mask)
        return (mask, taper) if taper is not None else mask

    return selection


def _layer_collections(root):
    yield root
    for child in root.children:
        yield from _layer_collections(child)


@contextmanager
def _temporarily_evaluable(carriers):
    collection_state = [(item, bool(item.hide_viewport))
                        for item in bpy.data.collections]
    layer_state = [(item, bool(item.hide_viewport))
                   for item in _layer_collections(
                       bpy.context.view_layer.layer_collection)]
    object_state = []
    for item in carriers:
        object_state.append((
            item,
            bool(item.hide_viewport),
            bool(item.hide_get()),
        ))
    try:
        for item, _hidden in collection_state:
            item.hide_viewport = False
        for item, _hidden in layer_state:
            item.hide_viewport = False
        for item, _viewport, _hidden in object_state:
            item.hide_viewport = False
            item.hide_set(False)
        bpy.context.view_layer.update()
        yield
    finally:
        for item, hidden in collection_state:
            item.hide_viewport = hidden
        for item, hidden in layer_state:
            item.hide_viewport = hidden
        for item, viewport, hidden in object_state:
            item.hide_viewport = viewport
            item.hide_set(hidden)
        bpy.context.view_layer.update()


def _parent_name(instance) -> str:
    parent = getattr(instance, "parent", None)
    if parent is None:
        return ""
    original = getattr(parent, "original", None)
    return str(getattr(original, "name", None) or getattr(parent, "name", ""))


def audit_visibility(scene, camera, carriers, *, min_pixels: float = 2.0,
                     occlusion_samples: int = 256) -> dict:
    """Count all and in-frustum instances; estimate readable unoccluded ones."""
    carriers = [item for item in carriers if item is not None]
    carrier_names = {item.name for item in carriers}
    resolution_x = max(1, int(scene.render.resolution_x))
    resolution_y = max(1, int(scene.render.resolution_y))
    tan_y = max(1e-6, math.tan(camera.data.angle_y / 2.0))
    origin, forward, _right, _up = camera_basis(camera)
    total = in_frustum = readable = 0
    reservoir = []
    rng = random.Random(0)
    readable_seen = 0

    # Prime the cache before carriers are made evaluable so occlusion rays use
    # actual scene surfaces, not a Geometry Nodes carrier.
    raycache.entries(scene)
    with _temporarily_evaluable(carriers):
        depsgraph = bpy.context.evaluated_depsgraph_get()
        for instance in depsgraph.object_instances:
            if not getattr(instance, "is_instance", False):
                continue
            if _parent_name(instance) not in carrier_names:
                continue
            total += 1
            location = instance.matrix_world.translation.copy()
            projected = world_to_camera_view(scene, camera, location)
            if not (0.0 <= projected.x <= 1.0
                    and 0.0 <= projected.y <= 1.0 and projected.z > 0.0):
                continue
            in_frustum += 1
            depth = max(1e-6, (location - origin).dot(forward))
            obj = instance.object
            local_size = max((float(value) for value in obj.dimensions), default=0.0)
            scale = max(instance.matrix_world.to_scale())
            radius = max(0.01, 0.5 * local_size * scale)
            projected_diameter = (
                2.0 * radius / depth * resolution_y / (2.0 * tan_y)
            )
            if projected_diameter < float(min_pixels):
                continue
            readable += 1
            readable_seen += 1
            sample = (location, radius)
            if len(reservoir) < occlusion_samples:
                reservoir.append(sample)
            else:
                replace = rng.randrange(readable_seen)
                if replace < occlusion_samples:
                    reservoir[replace] = sample

    visible_sample = 0
    for location, radius in reservoir:
        ray = location - origin
        distance = ray.length
        if distance <= 1e-6:
            visible_sample += 1
            continue
        hit = raycache.raycast(
            scene, origin + ray.normalized() * 0.02, ray,
            distance=distance + radius,
        )
        tolerance = max(0.25, radius)
        if hit is None or hit[0] >= distance - tolerance:
            visible_sample += 1
    if reservoir:
        visible_fraction = visible_sample / len(reservoir)
        effective = int(round(readable * visible_fraction))
    else:
        visible_fraction = 0.0
        effective = 0
    return {
        "total": total,
        "in_frustum": in_frustum,
        "screen_readable": readable,
        "effectively_visible": effective,
        "effectively_visible_estimated": readable > len(reservoir),
        "occlusion_sample_size": len(reservoir),
        "occlusion_visible_sample": visible_sample,
        "occlusion_visible_fraction": round(visible_fraction, 4),
        "min_projected_diameter_px": float(min_pixels),
        "resolution": [resolution_x, resolution_y],
    }
