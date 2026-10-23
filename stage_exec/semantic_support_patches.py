"""Shared helpers for indoor semantic support patches.

The patch schema groups the visible support cells into a small number of
plane-sized regions that are easier for a composer to reason about than a
dense cell grid.  The helpers stay pure-data so they can be reused by the
overlay builder, candidate selection, and offline gallery generation.
"""

from __future__ import annotations

from collections import defaultdict
import math


PLACEMENT_MODES_BY_KIND = {
    "floor": ("center", "edge", "back_to_wall", "row"),
    "tabletop": ("center", "edge"),
    "countertop": ("center", "edge"),
    "shelf": ("center", "edge"),
    "bed_top": ("center", "edge"),
    "seat": ("center", "edge", "row"),
}
SUPPORT_KIND_PRIORITY = (
    "floor", "tabletop", "countertop", "shelf", "bed_top", "seat",
)


def _point(value) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        x, y = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x) or not math.isfinite(y):
        return None
    return x, y


def _polygon_points(cell: dict, key: str) -> list[tuple[float, float]]:
    points = []
    for value in cell.get(key) or []:
        point = _point(value)
        if point is not None:
            points.append(point)
    return points


def convex_hull(points) -> list[list[float]]:
    """Return the convex hull of a 2D point cloud in counter-clockwise order."""
    unique = sorted({
        (round(float(point[0]), 8), round(float(point[1]), 8))
        for point in points
        if _point(point) is not None
    })
    if len(unique) <= 2:
        return [[float(x), float(y)] for x, y in unique]

    def cross(origin, left, right):
        return (
            (left[0] - origin[0]) * (right[1] - origin[1])
            - (left[1] - origin[1]) * (right[0] - origin[0])
        )

    lower = []
    for point in unique:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0.0:
            lower.pop()
        lower.append(point)
    upper = []
    for point in reversed(unique):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0.0:
            upper.pop()
        upper.append(point)
    hull = lower[:-1] + upper[:-1]
    return [[float(x), float(y)] for x, y in hull]


def polygon_centroid(points) -> list[float] | None:
    """Return an area centroid when possible, otherwise an arithmetic mean."""
    polygon = [point for point in ( _point(value) for value in points) if point]
    if not polygon:
        return None
    if len(polygon) < 3:
        x = sum(point[0] for point in polygon) / len(polygon)
        y = sum(point[1] for point in polygon) / len(polygon)
        return [float(x), float(y)]
    area = 0.0
    cx = 0.0
    cy = 0.0
    for index, (x0, y0) in enumerate(polygon):
        x1, y1 = polygon[(index + 1) % len(polygon)]
        cross = x0 * y1 - x1 * y0
        area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(area) <= 1e-9:
        x = sum(point[0] for point in polygon) / len(polygon)
        y = sum(point[1] for point in polygon) / len(polygon)
        return [float(x), float(y)]
    area *= 0.5
    return [float(cx / (6.0 * area)), float(cy / (6.0 * area))]


def polygon_bounds(points) -> list[float] | None:
    polygon = [point for point in ( _point(value) for value in points) if point]
    if not polygon:
        return None
    return [
        float(min(point[0] for point in polygon)),
        float(min(point[1] for point in polygon)),
        float(max(point[0] for point in polygon)),
        float(max(point[1] for point in polygon)),
    ]


def _support_kind(entry: dict, cells: list[dict]) -> str:
    types = {
        str(value) for value in (entry.get("surface_types") or [])
    }
    if not types:
        types = {
            str(value) for cell in cells for value in cell.get("surface_types") or []
        }
    for kind in SUPPORT_KIND_PRIORITY:
        if kind in types:
            return kind
    return str(entry.get("placement_category") or "floor")


def _default_modes(kind: str) -> list[str]:
    return list(PLACEMENT_MODES_BY_KIND.get(kind, ("center",)))


def _anchor_cell(cells: list[dict], mode: str) -> dict | None:
    usable = [
        cell for cell in cells
        if cell.get("screen_position") is not None
    ]
    if not usable:
        return None
    centroid = polygon_centroid(
        [cell.get("screen_position") for cell in usable]) or [0.5, 0.5]

    def clearance(cell):
        try:
            return int(cell.get("clearance_steps") or 0)
        except (TypeError, ValueError):
            return 0

    def screen(cell):
        point = cell.get("screen_position") or [0.5, 0.5]
        return float(point[0]), float(point[1])

    if mode == "back_to_wall":
        return min(
            usable,
            key=lambda cell: (
                -screen(cell)[1],
                abs(screen(cell)[0] - centroid[0]),
                -clearance(cell),
                str(cell.get("cell_id") or ""),
            ),
        )
    if mode == "edge":
        return min(
            usable,
            key=lambda cell: (
                clearance(cell),
                -math.dist(screen(cell), centroid),
                str(cell.get("cell_id") or ""),
            ),
        )
    return min(
        usable,
        key=lambda cell: (
            math.dist(screen(cell), centroid),
            -clearance(cell),
            str(cell.get("cell_id") or ""),
        ),
    )


def build_support_patches(data: dict) -> list[dict]:
    """Group visible feasible cells into plane-sized support patches."""
    cells = [cell for cell in data.get("cells", [])
             if cell.get("status") == "feasible"]
    inventory_by_plane = {
        str(entry.get("plane_id") or entry.get("support_plane_id") or ""): entry
        for entry in data.get("surface_inventory") or []
        if str(entry.get("plane_id") or entry.get("support_plane_id") or "")
    }
    by_plane: dict[str, list[dict]] = defaultdict(list)
    for cell in cells:
        plane_id = str(cell.get("support_plane_id") or cell.get("region_id") or "")
        if plane_id:
            by_plane[plane_id].append(cell)

    patches = []
    for plane_id, plane_cells in sorted(by_plane.items(), key=lambda item: item[0]):
        inventory = dict(inventory_by_plane.get(plane_id, {}))
        surface_types = sorted({
            str(value) for cell in plane_cells
            for value in cell.get("surface_types") or []
        })
        support_kind = _support_kind(inventory, plane_cells)
        screen_polygon = convex_hull(
            point for cell in plane_cells
            for point in _polygon_points(cell, "screen_polygon")
        )
        if not screen_polygon:
            screen_polygon = convex_hull(
                cell.get("screen_position") for cell in plane_cells)
        world_xy_polygon = convex_hull(
            point for cell in plane_cells
            for point in _polygon_points(cell, "world_xy_polygon")
        )
        if not world_xy_polygon:
            world_xy_polygon = convex_hull(
                cell.get("world_position")[:2]
                for cell in plane_cells
                if cell.get("world_position") is not None
            )
        screen_bounds = polygon_bounds(screen_polygon)
        world_xy_bounds = polygon_bounds(world_xy_polygon)
        center_cell = _anchor_cell(plane_cells, "center")
        edge_cell = _anchor_cell(plane_cells, "edge")
        back_cell = _anchor_cell(plane_cells, "back_to_wall")
        max_clearance = max(
            (int(cell.get("clearance_steps") or 0) for cell in plane_cells),
            default=0)
        patches.append({
            "patch_id": plane_id,
            "support_plane_id": plane_id,
            "support_kind": support_kind,
            "placement_modes": _default_modes(support_kind),
            "default_placement_mode": "center",
            "patch_label": " ".join(part for part in (
                plane_id,
                support_kind,
                str(inventory.get("surface_object") or "").split(".", 1)[0],
            ) if part),
            "surface_types": surface_types,
            "surface_objects": list(dict.fromkeys(
                str(value) for value in inventory.get("surface_objects") or [
                    cell.get("hit_surface") for cell in plane_cells
                ] if value)),
            "visible_cell_count": int(
                inventory.get("visible_cell_count") or len(plane_cells)),
            "usable_cell_count": int(
                inventory.get("usable_cell_count")
                or sum(1 for cell in plane_cells if cell.get("status") == "feasible")),
            "cell_count": len(plane_cells),
            "cell_ids": [cell.get("cell_id") for cell in plane_cells
                         if cell.get("cell_id") is not None],
            "usable_cell_ids": [cell.get("cell_id") for cell in plane_cells
                                if cell.get("cell_id") is not None],
            "screen_polygon": screen_polygon,
            "world_xy_polygon": world_xy_polygon,
            "screen_bounds": screen_bounds,
            "world_xy_bounds": world_xy_bounds,
            "screen_centroid": polygon_centroid(screen_polygon),
            "world_xy_centroid": polygon_centroid(world_xy_polygon),
            "median_plane_quality_score": inventory.get(
                "median_plane_quality_score"),
            "max_plane_quality_score": inventory.get("max_plane_quality_score"),
            "max_clearance_steps": int(max_clearance),
            "estimated_visible_area_m2": inventory.get(
                "estimated_visible_area_m2"),
            "estimated_usable_area_m2": inventory.get(
                "estimated_usable_area_m2"),
            "anchor_cell_ids": {
                "center": None if center_cell is None else center_cell.get("cell_id"),
                "edge": None if edge_cell is None else edge_cell.get("cell_id"),
                "back_to_wall": None if back_cell is None else back_cell.get("cell_id"),
            },
        })
    return patches


def selected_patch_ids(selected_rows: list[dict]) -> set[str]:
    ids = set()
    for row in selected_rows:
        for key in ("support_patch_id", "patch_id", "support_plane_id", "region_id"):
            value = row.get(key)
            if value is not None:
                ids.add(str(value))
                break
    return ids


def patch_anchor_cell_id(patch: dict, mode: str | None = None) -> str | None:
    anchors = patch.get("anchor_cell_ids") or {}
    requested = str(mode or patch.get("default_placement_mode") or "center")
    if requested in anchors and anchors.get(requested):
        return str(anchors.get(requested))
    if anchors.get("center"):
        return str(anchors["center"])
    for key in ("edge", "back_to_wall"):
        if anchors.get(key):
            return str(anchors[key])
    return None
