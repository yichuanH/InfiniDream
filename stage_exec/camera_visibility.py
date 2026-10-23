"""Detect-only camera, projected-box and animated collision audits.

This module is imported only from Blender processes.  It deliberately checks the
evaluated render geometry, not placeholder origins: a root can be inside the
frustum while a tall or offset asset is completely outside it.
"""

from __future__ import annotations

import math
import os

RENDER_GEOMETRY_TYPES = {"MESH", "CURVE", "SURFACE", "META", "FONT"}
# Outdoor Composer review is intentionally loose: a placement only needs to be
# visible in roughly a third of the deliverable frames to survive the hard gate.
MIN_REQUESTED_VISIBLE_FRAME_FRACTION = 1.0 / 3.0
MIN_PROJECTED_OVERLAP = 0.10
MIN_PROJECTED_AREA = 0.00004
# Camera-space overlap is not collision: ordinary foreground/background
# composition naturally overlaps. Only very heavy occlusion is worth reporting,
# and even then it is a soft composition warning.
# A batch may raise it (BI_MAX_PROJECTED_OVERLAP); the default stays 0.70.
MAX_REQUESTED_BOX_OVERLAP = float(os.environ.get("BI_MAX_PROJECTED_OVERLAP", "0.70"))
# Batch opt-in: a requested asset only needs to be partly in frame (see projected_bounds).
PARTIAL_IN_FRAME_OK = os.environ.get("BI_PARTIAL_IN_FRAME_OK") == "1"
# Batch opt-in (BI_PLACEMENT_EXTENDED_GRID>1): objects may be placed off-screen, so
# the review / placer / final-contract "must be visible in frame" gates do not apply.
try:
    EXTENDED_GRID = float(os.environ.get("BI_PLACEMENT_EXTENDED_GRID", "1") or 1) > 1.0
except ValueError:
    EXTENDED_GRID = False
MIN_WORLD_COLLISION_FRACTION = 0.01


def minimum_visible_frame_count(frame_count: int, *,
                                min_visible_fraction: float = (
                                    MIN_REQUESTED_VISIBLE_FRAME_FRACTION)) -> int:
    frame_count = int(frame_count)
    if frame_count <= 0:
        return 0
    return max(1, math.ceil(frame_count * float(min_visible_fraction)))


def _anchor_factory_name(anchor) -> str | None:
    factory = str(anchor.get("bi_requested_factory") or "")
    if factory:
        return factory
    fqn = str(anchor.get("bi_populated_factory_fqn") or "")
    if fqn:
        return fqn.rsplit(".", 1)[-1]
    return None


def rectangle_overlap_of_smaller(a, b) -> float:
    """Intersection area divided by the smaller rectangle's area."""
    if not a or not b or len(a) != 4 or len(b) != 4:
        return 0.0
    ax0, ay0, ax1, ay1 = map(float, a)
    bx0, by0, bx1, by1 = map(float, b)
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    smaller = min(area_a, area_b)
    if smaller <= 0.0:
        return 0.0
    intersection = (
        max(0.0, min(ax1, bx1) - max(ax0, bx0))
        * max(0.0, min(ay1, by1) - max(ay0, by0))
    )
    return float(intersection / smaller)


def projected_overlap_violations(items, *, threshold=MAX_REQUESTED_BOX_OVERLAP) -> list[dict]:
    """Pure-data pairwise audit over every shared frame in saved anchor audits.

    ``items`` entries contain ``edit_id``, ``object`` and an ``audit`` produced by
    :func:`audit_anchor`.  This deliberately measures requested boxes only; existing
    scene placeholders are context, not collision geometry.
    """
    normalized = []
    for item in items:
        samples = {
            int(sample["frame"]): sample
            for sample in (item.get("audit") or {}).get("samples", [])
            if sample.get("frame") is not None
        }
        normalized.append((item, samples))
    violations = []
    for index, (left, left_samples) in enumerate(normalized):
        for right, right_samples in normalized[index + 1:]:
            hits = []
            maximum = 0.0
            for frame in sorted(set(left_samples) & set(right_samples)):
                ratio = rectangle_overlap_of_smaller(
                    left_samples[frame].get("bounds_ndc"),
                    right_samples[frame].get("bounds_ndc"),
                )
                maximum = max(maximum, ratio)
                if ratio > float(threshold):
                    hits.append({"frame": frame, "overlap_of_smaller": ratio})
            if hits:
                violations.append({
                    "type": "projected_overlap",
                    "edit_ids": [left.get("edit_id"), right.get("edit_id")],
                    "objects": [left.get("object"), right.get("object")],
                    "threshold": float(threshold),
                    "overlap_frames": len(hits),
                    "frames": [hit["frame"] for hit in hits],
                    "first_frame": hits[0]["frame"],
                    "last_frame": hits[-1]["frame"],
                    "max_overlap_of_smaller": float(maximum),
                })
    return violations


def _collection_visibility(scene) -> dict[str, bool]:
    visible = {}

    def walk(collection, parent_visible):
        here = bool(parent_visible and not collection.hide_render)
        visible[collection.name] = visible.get(collection.name, False) or here
        for child in collection.children:
            walk(child, here)

    walk(scene.collection, True)
    return visible


def _render_geometry(scene, anchor):
    collection_visible = _collection_visibility(scene)
    objects = [anchor] + list(anchor.children_recursive)
    return [
        obj for obj in objects
        if obj.type in RENDER_GEOMETRY_TYPES
        and not obj.hide_render
        and any(collection_visible.get(coll.name, False) for coll in obj.users_collection)
    ]


def _world_bbox_points(scene, anchor, *, visible_only=True):
    """Evaluated world-space bounding-box corners for visible asset geometry."""
    from mathutils import Vector
    import bpy

    depsgraph = bpy.context.evaluated_depsgraph_get()
    points = []
    objects = _render_geometry(scene, anchor) if visible_only else [
        obj for obj in [anchor] + list(anchor.children_recursive)
        if obj.type in RENDER_GEOMETRY_TYPES
    ]
    for obj in objects:
        evaluated = obj.evaluated_get(depsgraph)
        matrix = evaluated.matrix_world
        try:
            points.extend(matrix @ Vector(corner) for corner in evaluated.bound_box)
        except (AttributeError, TypeError, ValueError):
            continue
    return points


def projected_bounds(scene, anchor, *, visible_only=True) -> dict:
    """Project evaluated geometry into the active camera at the current frame."""
    from bpy_extras.object_utils import world_to_camera_view
    from mathutils import Vector

    camera = scene.camera
    if camera is None:
        return {"in_view": False, "reason": "scene has no active camera"}
    points = _world_bbox_points(scene, anchor, visible_only=visible_only)
    if not points:
        return {"in_view": False, "reason": "no render-visible evaluated geometry"}

    lo = Vector((min(p.x for p in points), min(p.y for p in points), min(p.z for p in points)))
    hi = Vector((max(p.x for p in points), max(p.y for p in points), max(p.z for p in points)))
    center_world = (lo + hi) * 0.5
    center = world_to_camera_view(scene, camera, center_world)
    projected = [world_to_camera_view(scene, camera, point) for point in points]
    front = [co for co in projected if co.z > 0.0]
    if not front or center.z <= 0.0:
        return {
            "in_view": False,
            "reason": "geometry center is behind the camera",
            "center_world": [float(v) for v in center_world],
            "center_ndc": [float(center.x), float(center.y), float(center.z)],
        }

    min_x, max_x = min(co.x for co in front), max(co.x for co in front)
    min_y, max_y = min(co.y for co in front), max(co.y for co in front)
    width = max(1e-12, float(max_x - min_x))
    height = max(1e-12, float(max_y - min_y))
    overlap_x = max(0.0, min(1.0, float(max_x)) - max(0.0, float(min_x)))
    overlap_y = max(0.0, min(1.0, float(max_y)) - max(0.0, float(min_y)))
    overlap = min(1.0, (overlap_x * overlap_y) / (width * height))
    center_inside = 0.0 <= center.x <= 1.0 and 0.0 <= center.y <= 1.0
    fully_in_frame = bool(
        min_x >= 0.0 and max_x <= 1.0 and min_y >= 0.0 and max_y <= 1.0)
    # Reject assets so distant that their evaluated bbox is effectively a dot.
    # This is still a geometric proxy rather than an occlusion/pixel-segmentation
    # proof, but it prevents "technically in frustum, visually absent" passes.
    projected_area = overlap_x * overlap_y
    min_area = MIN_PROJECTED_AREA
    in_view = bool((center_inside or PARTIAL_IN_FRAME_OK)
                   and overlap >= MIN_PROJECTED_OVERLAP
                   and projected_area >= min_area)
    if PARTIAL_IN_FRAME_OK:
        fully_in_frame = in_view
    if in_view:
        reason = None
    elif projected_area < min_area:
        reason = "projected geometry is too small in the camera frame"
    else:
        reason = "projected geometry is outside the camera frame"
    return {
        "in_view": in_view,
        "reason": reason,
        "center_world": [float(v) for v in center_world],
        "center_ndc": [float(center.x), float(center.y), float(center.z)],
        "bounds_ndc": [float(min_x), float(min_y), float(max_x), float(max_y)],
        "overlap_fraction": float(overlap),
        "projected_area_fraction": float(projected_area),
        "fully_in_frame": fully_in_frame,
    }


def world_bounds(scene, anchor, *, visible_only=True) -> dict | None:
    """Evaluated world AABB used as a conservative animated collision proxy."""
    points = _world_bbox_points(scene, anchor, visible_only=visible_only)
    if not points:
        return None
    return {
        "min": [float(min(getattr(point, axis) for point in points))
                for axis in ("x", "y", "z")],
        "max": [float(max(getattr(point, axis) for point in points))
                for axis in ("x", "y", "z")],
    }


def box_overlap_of_smaller(a, b) -> float:
    """Intersection volume divided by the smaller AABB volume."""
    if not a or not b:
        return 0.0
    amin, amax = a.get("min"), a.get("max")
    bmin, bmax = b.get("min"), b.get("max")
    if not amin or not amax or not bmin or not bmax:
        return 0.0
    size_a = [max(0.0, float(amax[i]) - float(amin[i])) for i in range(3)]
    size_b = [max(0.0, float(bmax[i]) - float(bmin[i])) for i in range(3)]
    volume_a = size_a[0] * size_a[1] * size_a[2]
    volume_b = size_b[0] * size_b[1] * size_b[2]
    smaller = min(volume_a, volume_b)
    if smaller <= 1e-12:
        return 0.0
    intersection = 1.0
    for axis in range(3):
        intersection *= max(
            0.0,
            min(float(amax[axis]), float(bmax[axis]))
            - max(float(amin[axis]), float(bmin[axis])),
        )
    return float(intersection / smaller)


def audit_world_collisions(scene, anchors, frames=None, *,
                           threshold=MIN_WORLD_COLLISION_FRACTION,
                           same_factory_only=False) -> dict:
    """Check populated requested assets pairwise at every deliverable frame.

    This is a conservative broad-phase audit (evaluated world AABBs), not a mesh
    penetration solver.  It never mutates paths or base positions.
    """
    anchors = list(anchors)
    factory_names = {
        anchor.name: _anchor_factory_name(anchor)
        for anchor in anchors
    }
    if frames is None:
        frames = range(int(scene.frame_start), int(scene.frame_end) + 1)
    frames = [int(frame) for frame in frames]
    original_frame = int(scene.frame_current)
    pairs = {}
    try:
        for frame in frames:
            scene.frame_set(frame)
            import bpy
            bpy.context.view_layer.update()
            bounds = {anchor.name: world_bounds(scene, anchor) for anchor in anchors}
            for index, left in enumerate(anchors):
                for right in anchors[index + 1:]:
                    left_factory = factory_names.get(left.name)
                    right_factory = factory_names.get(right.name)
                    if same_factory_only:
                        if not left_factory or left_factory != right_factory:
                            continue
                    ratio = box_overlap_of_smaller(
                        bounds.get(left.name), bounds.get(right.name))
                    if ratio <= float(threshold):
                        continue
                    key = (left.name, right.name)
                    pair = pairs.setdefault(key, {
                        "type": "animated_world_aabb_collision",
                        "objects": [left.name, right.name],
                        "edit_ids": [
                            left.get("bi_requested_code_from") or left.get("bi_code_from"),
                            right.get("bi_requested_code_from") or right.get("bi_code_from"),
                        ],
                        "placement_indices": [
                            left.get("bi_requested_placement_index", left.get("bi_instance_index")),
                            right.get("bi_requested_placement_index", right.get("bi_instance_index")),
                        ],
                        "factories": [left_factory, right_factory],
                        "threshold": float(threshold),
                        "frames": [],
                        "max_overlap_of_smaller": 0.0,
                    })
                    if same_factory_only:
                        pair["factory"] = left_factory
                    pair["same_factory_only"] = bool(same_factory_only)
                    if not pair["frames"]:
                        pair["screen_at_first_collision"] = {
                            left.name: projected_bounds(scene, left),
                            right.name: projected_bounds(scene, right),
                        }
                    pair["frames"].append(frame)
                    pair["max_overlap_of_smaller"] = max(
                        pair["max_overlap_of_smaller"], ratio)
    finally:
        scene.frame_set(original_frame)
        import bpy
        bpy.context.view_layer.update()
    violations = []
    for pair in pairs.values():
        pair["collision_frames"] = len(pair["frames"])
        pair["first_frame"] = pair["frames"][0]
        pair["last_frame"] = pair["frames"][-1]
        violations.append(pair)
    return {
        "proxy": "evaluated world-space axis-aligned bounding boxes",
        "frame_range": [frames[0], frames[-1]] if frames else None,
        "anchor_count": len(anchors),
        "same_factory_only": bool(same_factory_only),
        "violations": violations,
        "ok": not violations,
    }


def audit_anchor(scene, anchor, frames=None, *, visible_only=True) -> dict:
    if frames is None:
        frames = range(int(scene.frame_start), int(scene.frame_end) + 1)
    frames = [int(frame) for frame in frames]
    original_frame = int(scene.frame_current)
    samples = []
    try:
        for frame in frames:
            scene.frame_set(frame)
            import bpy
            bpy.context.view_layer.update()
            sample = projected_bounds(scene, anchor, visible_only=visible_only)
            sample["frame"] = frame
            samples.append(sample)
    finally:
        scene.frame_set(original_frame)
        import bpy
        bpy.context.view_layer.update()
    visible = sum(bool(sample.get("in_view")) for sample in samples)
    fully_visible = sum(bool(sample.get("in_view")
                             and sample.get("fully_in_frame")) for sample in samples)
    return {
        "object": anchor.name,
        "frame_count": len(samples),
        "visible_frames": visible,
        "all_frames_visible": bool(samples) and visible == len(samples),
        "fully_visible_frames": fully_visible,
        "all_frames_fully_visible": bool(samples) and fully_visible == len(samples),
        "samples": samples,
    }


def snapshot_requested_visibility(scene, *,
                                  min_visible_fraction=(
                                      MIN_REQUESTED_VISIBLE_FRAME_FRACTION)
                                  ) -> dict:
    """Audit every requested anchor at every deliverable frame in this exact blend."""
    anchors = sorted(
        (obj for obj in scene.objects if obj.get("bi_requested_placement_index") is not None),
        key=lambda obj: (int(obj["bi_requested_placement_index"]), obj.name),
    )
    placements = {}
    failures = []
    required_visible_frames = minimum_visible_frame_count(
        int(scene.frame_end) - int(scene.frame_start) + 1,
        min_visible_fraction=min_visible_fraction,
    )
    for anchor in anchors:
        index = int(anchor["bi_requested_placement_index"])
        audit = audit_anchor(scene, anchor)
        placements.setdefault(str(index), []).append(audit)
        if audit["visible_frames"] < required_visible_frames:
            failures.append(
                f"placement #{index} {anchor.name}: "
                f"visible {audit['visible_frames']}/{audit['frame_count']} frames "
                f"(need >= {required_visible_frames})")
    return {
        "frame_range": [int(scene.frame_start), int(scene.frame_end)],
        "anchor_count": len(anchors),
        "min_visible_fraction": float(min_visible_fraction),
        "minimum_visible_frames": int(required_visible_frames),
        "placements": placements,
        "failures": failures,
    }
