"""Shared Blender-side placeholder population intent helpers."""

from __future__ import annotations

import math
import os

import bpy
import numpy as np
from mathutils import Vector


def roots_of(collection):
    return [obj for obj in collection.objects if obj.parent is None]


def _finite_distance(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number >= 1e6:
        return None
    return number


def _camera_centers_by_frame(cameras) -> list:
    if not cameras:
        return []
    scene = bpy.context.scene
    original_frame = int(scene.frame_current)
    centers = []
    try:
        for frame in range(int(scene.frame_start), int(scene.frame_end) + 1):
            scene.frame_set(frame)
            bpy.context.view_layer.update()
            for camera in cameras:
                centers.append(camera.matrix_world.translation.copy())
    finally:
        scene.frame_set(original_frame)
        bpy.context.view_layer.update()
    return centers


def _nearest_camera_center_distances(roots, cameras) -> dict[str, float]:
    centers = _camera_centers_by_frame(cameras)
    if not centers:
        return {}
    distances = {}
    for obj in roots:
        point = obj.matrix_world.translation
        distances[obj.name] = min(float((point - center).length)
                                  for center in centers)
    return distances


def _placeholder_sample_points(obj) -> np.ndarray:
    try:
        corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    except Exception:
        corners = []
    points = []
    if corners:
        points.extend(corners)
        center = sum(corners, corners[0].copy() * 0.0) / len(corners)
        points.append(center)
    else:
        points.append(obj.matrix_world.translation.copy())
    return np.array([[float(point.x), float(point.y), float(point.z)]
                     for point in points])


def _sampled_population_targets(roots, cameras, dist_cull, vis_cull):
    if not cameras:
        return []
    from infinigen.core.placement import split_in_view

    chunks = []
    spans = []
    cursor = 0
    for placeholder in roots:
        points = _placeholder_sample_points(placeholder)
        chunks.append(points)
        spans.append((placeholder, cursor, cursor + len(points)))
        cursor += len(points)
    if not chunks:
        return []

    all_points = np.concatenate(chunks, axis=0)
    mask, min_dists, min_vis_dists = split_in_view.compute_inview_distances(
        all_points,
        list(cameras),
        dist_max=dist_cull,
        vis_margin=vis_cull,
        verbose=False,
    )
    targets = []
    for placeholder, start, end in spans:
        local = mask[start:end]
        if not local.any():
            continue
        visible_indices = np.flatnonzero(local) + start
        targets.append((
            placeholder,
            float(min_dists[visible_indices].min()),
            float(min_vis_dists[visible_indices].min()),
        ))
    return targets


def population_target_metadata(
        roots, cameras, *, dist_cull, vis_cull, include_all: bool = False,
        fallback_distance: float = 4.0) -> dict[str, dict]:
    """Predict populate targets and cache the distance values used for LOD."""
    from infinigen.core.placement import placement as _pl

    scene = bpy.context.scene
    original_frame = int(scene.frame_current)
    result: dict[str, dict] = {}
    try:
        use_exact = os.environ.get("PLACEMENT_EXACT_POPULATION_CULL") == "1"
        try:
            if use_exact:
                targets = _pl.filter_populate_targets(
                    list(roots), list(cameras or []), dist_cull, vis_cull, False)
            else:
                targets = _sampled_population_targets(
                    list(roots), list(cameras or []), dist_cull, vis_cull)
        except Exception:
            if not include_all:
                raise
            targets = []
        visible_names = {placeholder.name for placeholder, _dist, _vis_dist in targets}
        for placeholder, dist, vis_dist in targets:
            result[placeholder.name] = {
                "object": placeholder.name,
                "will_populate": True,
                "coarse_cull_visible": True,
                "dist_m": _finite_distance(dist) or fallback_distance,
                "vis_dist_m": _finite_distance(vis_dist) or 0.0,
                "distance_source": (
                    "coarse_filter_populate_targets"
                    if use_exact else "coarse_bbox_filter"),
            }
        if include_all:
            missing_roots = [
                placeholder for placeholder in roots
                if placeholder.name not in visible_names
            ]
            fallback_distances = _nearest_camera_center_distances(
                missing_roots, cameras)
            for placeholder in roots:
                if placeholder.name in result:
                    continue
                result[placeholder.name] = {
                    "object": placeholder.name,
                    "will_populate": True,
                    "coarse_cull_visible": False,
                    "dist_m": (
                        fallback_distances.get(placeholder.name)
                        or fallback_distance),
                    "vis_dist_m": None,
                    "distance_source": "coarse_camera_center_fallback",
                }
    finally:
        scene.frame_set(original_frame)
        bpy.context.view_layer.update()
    return result


def force_population_target_metadata(
        roots, cameras, visible_rows_by_name: dict[str, dict] | None = None,
        *, fallback_distance: float = 4.0) -> dict[str, dict]:
    """Return metadata for roots that Composer explicitly materializes."""
    result = {}
    visible_rows_by_name = visible_rows_by_name or {}
    for placeholder in roots:
        row = visible_rows_by_name.get(placeholder.name)
        if not row:
            continue
        copied = dict(row)
        copied["will_populate"] = True
        copied["coarse_cull_visible"] = bool(
            copied.get("coarse_cull_visible", True))
        result[placeholder.name] = copied

    missing_roots = [
        placeholder for placeholder in roots
        if placeholder.name not in result
    ]
    fallback_distances = _nearest_camera_center_distances(missing_roots, cameras)
    for placeholder in missing_roots:
        result[placeholder.name] = {
            "object": placeholder.name,
            "will_populate": True,
            "coarse_cull_visible": False,
            "dist_m": (
                fallback_distances.get(placeholder.name)
                or fallback_distance),
            "vis_dist_m": None,
            "distance_source": "coarse_camera_center_fallback",
        }
    return result


def row_public_population_targets(rows_by_name: dict[str, dict],
                                  names: list[str] | None = None) -> list[dict]:
    """Return compact JSON metadata in stable placeholder order."""
    ordered = names or sorted(rows_by_name)
    rows = []
    for name in ordered:
        row = rows_by_name.get(name)
        if not row:
            continue
        public = {
            "object": row.get("object") or name,
            "will_populate": bool(row.get("will_populate")),
            "coarse_cull_visible": bool(
                row.get("coarse_cull_visible", row.get("will_populate"))),
            "dist_m": row.get("dist_m"),
            "vis_dist_m": row.get("vis_dist_m"),
            "distance_source": row.get("distance_source"),
        }
        rows.append(public)
    return rows
