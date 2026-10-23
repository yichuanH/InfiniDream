#!/usr/bin/env python
"""Render a deterministic world-XY planning map from feasibility JSON."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


STATUS_COLORS = {
    "feasible": (58, 221, 114, 235),
    "surface": (42, 139, 224, 210),
    "slope": (230, 72, 72, 220),
    "footprint": (245, 157, 45, 220),
    "boundary": (196, 92, 224, 220),
    "obstacle": (225, 205, 92, 230),
    "terrain": (186, 104, 62, 225),
    "no_water_voxel": (73, 82, 96, 190),
    "no_air_voxel": (73, 82, 96, 190),
    "no_hit": (86, 91, 101, 180),
}


def font(size: int, *, bold=True):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(
            f"/usr/share/fonts/truetype/dejavu/{name}", size)
    except OSError:
        return ImageFont.load_default()


def _world_bounds(data: dict) -> tuple[float, float, float, float]:
    points = [cell["world_position"][:2] for cell in data.get("cells", [])
              if cell.get("world_position") is not None]
    for cell in data.get("cells", []):
        points.extend(point[:2] for point in (cell.get("world_xy_polygon") or []))
    camera = (data.get("camera") or {}).get("world_position")
    if camera:
        points.append(camera[:2])
    for point in (data.get("camera") or {}).get("frustum_world_xy", []):
        points.append(point[:2])
    for obstacle in data.get("obstacles", []):
        bounds = obstacle.get("world_xy_bounds")
        if bounds:
            points.extend(([bounds[0], bounds[1]], [bounds[2], bounds[3]]))
    if not points:
        return (-1.0, -1.0, 1.0, 1.0)
    xmin = min(point[0] for point in points)
    ymin = min(point[1] for point in points)
    xmax = max(point[0] for point in points)
    ymax = max(point[1] for point in points)
    span = max(xmax - xmin, ymax - ymin, 2.0)
    margin = span * 0.08
    return xmin - margin, ymin - margin, xmax + margin, ymax + margin


def render_region_map(data: dict, output: Path) -> None:
    """Render an undistorted screen heatmap beside a filled semantic top map."""
    width, height = 2000, 1120
    header, footer = 92, 105
    left_panel = (28, 132, 978, height - footer - 12)
    right_panel = (1012, 132, width - 28, height - footer - 12)
    image = Image.new("RGBA", (width, height), (19, 23, 29, 255))
    draw = ImageDraw.Draw(image, "RGBA")
    title_font = font(30)
    panel_font = font(21)
    label_font = font(16)
    small_font = font(14, bold=False)
    tiny_font = font(12, bold=False)
    overlay = data.get("selection_overlay") or {}
    excluded_ids = {str(row.get("cell_id"))
                    for row in overlay.get("excluded", [])
                    if row.get("cell_id")}
    reserved_ids = {str(row.get("cell_id"))
                    for row in overlay.get("cross_edit_reserved", [])
                    if row.get("cell_id")}
    selected_rows = [row for row in overlay.get("selected", [])
                     if row.get("cell_id")]
    selected_by_cell = {str(row["cell_id"]): row for row in selected_rows}

    xmin, ymin, xmax, ymax = _world_bounds(data)
    span_x, span_y = max(xmax - xmin, 1e-6), max(ymax - ymin, 1e-6)
    world_left, world_top, world_right, world_bottom = right_panel
    world_top += 45
    usable_w = world_right - world_left
    usable_h = world_bottom - world_top
    scale = min(usable_w / span_x, usable_h / span_y)
    offset_x = world_left + (usable_w - span_x * scale) * 0.5
    offset_y = world_top + (usable_h - span_y * scale) * 0.5

    def world_project(point):
        return (
            offset_x + (float(point[0]) - xmin) * scale,
            offset_y + (ymax - float(point[1])) * scale,
        )

    draw.rectangle((0, 0, width, header), fill=(13, 16, 21, 255))
    title = f"PLACEMENT PLANNING MAP — {data.get('edit_id', 'UNKNOWN')}"
    draw.text((24, 13), title, font=title_font, fill=(255, 255, 255, 255))
    subtitle = (
        f"semantic target: {data.get('semantic_target', '')}   |   "
        "camera coordinates use BOTTOM-LEFT origin")
    draw.text((25, 56), subtitle, font=small_font,
              fill=(206, 213, 224, 255))
    for panel in (left_panel, right_panel):
        draw.rounded_rectangle(panel, radius=8, fill=(27, 33, 41, 255),
                               outline=(104, 114, 128, 255), width=2)
    draw.text((left_panel[0] + 18, left_panel[1] + 12),
              "CAMERA-SCREEN FEASIBILITY", font=panel_font,
              fill=(244, 247, 251, 255))
    anchoring = data.get("anchoring")
    world_panel_title = (
        "WORLD-XY 3D WATER-VOXEL MAP" if anchoring == "submerged"
        else "WORLD-XY 3D AIR-FRUSTUM VOXEL MAP" if anchoring == "airborne"
        else "WORLD-XY SEMANTIC TOP MAP")
    draw.text((right_panel[0] + 18, right_panel[1] + 12),
              world_panel_title, font=panel_font,
              fill=(244, 247, 251, 255))

    def screen_project(point, rect, bounds):
        rx0, ry0, rx1, ry1 = rect
        sx0, sy0, sx1, sy1 = bounds
        return (
            rx0 + (float(point[0]) - sx0) / max(sx1 - sx0, 1e-9) * (rx1 - rx0),
            ry1 - (float(point[1]) - sy0) / max(sy1 - sy0, 1e-9) * (ry1 - ry0),
        )

    # Full final-camera inset: composition context for the enlarged bounds map.
    full_rect = (left_panel[0] + 36, left_panel[1] + 72,
                 left_panel[0] + 466, left_panel[1] + 314)
    screen_bounds = list(map(float, data.get("screen_bounds", [0, 0, 1, 1])))
    bx0, by0, bx1, by1 = screen_bounds
    draw.rectangle(full_rect, fill=(13, 17, 23, 255),
                   outline=(191, 200, 213, 255), width=2)
    bound_left, bound_bottom = screen_project(
        (bx0, by0), full_rect, (0.0, 0.0, 1.0, 1.0))
    bound_right, bound_top = screen_project(
        (bx1, by1), full_rect, (0.0, 0.0, 1.0, 1.0))
    draw.rectangle((bound_left, bound_top, bound_right, bound_bottom),
                   fill=(99, 167, 255, 34),
                   outline=(118, 185, 255, 255), width=3)
    draw.text((full_rect[0], full_rect[1] - 23), "FULL FINAL-CAMERA FRAME",
              font=small_font, fill=(196, 205, 218, 255))
    draw.text((bound_left + 5, bound_top + 5), "DIRECTOR BOUNDS",
              font=tiny_font, fill=(176, 215, 255, 255))
    draw.text((full_rect[0] + 7, full_rect[3] - 22), "(0,0)",
              font=tiny_font, fill=(156, 166, 181, 255))
    draw.text((full_rect[2] - 39, full_rect[1] + 6), "(1,1)",
              font=tiny_font, fill=(156, 166, 181, 255))

    instruction_x = full_rect[2] + 28
    instruction_y = full_rect[1] + 4
    instructions = [
        "Bottom-left camera coordinates",
        "x → right     y → up",
        "Large map below = bounds zoom",
        "FAILED = old rejected point",
        "Code samples feasible regions",
        "Arrangement-aware code selects cells",
    ]
    for line_index, line in enumerate(instructions):
        draw.text((instruction_x, instruction_y + line_index * 30), line,
                  font=label_font if line_index == 0 else small_font,
                  fill=((246, 249, 253, 255) if line_index == 0
                        else (190, 199, 212, 255)))

    # Large, undistorted crop of the Director placement bounds.
    zoom_left = left_panel[0] + 36
    zoom_right = left_panel[2] - 36
    zoom_width = zoom_right - zoom_left
    display_aspect = ((bx1 - bx0) * 16.0 / 9.0) / max(by1 - by0, 1e-9)
    zoom_height = min(330.0, zoom_width / max(display_aspect, 1e-9))
    zoom_top = left_panel[1] + 405
    zoom_rect = (zoom_left, zoom_top, zoom_right, zoom_top + zoom_height)
    columns = int(data.get("grid", {}).get("columns", 1))
    rows = int(data.get("grid", {}).get("rows", 1))
    draw.text((zoom_left, zoom_top - 28),
              f"PLACEMENT BOUNDS — {columns}×{rows} MACHINE GRID",
              font=small_font, fill=(210, 217, 228, 255))
    draw.rectangle(zoom_rect, fill=(10, 14, 19, 255),
                   outline=(207, 215, 226, 255), width=2)
    step_x = (bx1 - bx0) / max(columns, 1)
    step_y = (by1 - by0) / max(rows, 1)
    for cell in data.get("cells", []):
        cx, cy = map(float, cell["screen_position"])
        x0, y1 = screen_project(
            (cx - step_x / 2, cy - step_y / 2), zoom_rect, screen_bounds)
        x1, y0 = screen_project(
            (cx + step_x / 2, cy + step_y / 2), zoom_rect, screen_bounds)
        color = ((105, 112, 123, 235) if cell["cell_id"] in excluded_ids
                 else (78, 91, 120, 235)
                 if cell["cell_id"] in reserved_ids
                 else STATUS_COLORS.get(
                     cell.get("status"), (160, 160, 160, 220)))
        draw.rectangle((x0, y0, x1, y1), fill=color,
                       outline=(9, 12, 17, 95), width=1)

    def failed_marker(point, rect, bounds, label):
        x, y = screen_project(point, rect, bounds)
        draw.line((x - 9, y - 9, x + 9, y + 9),
                  fill=(255, 55, 55, 255), width=4)
        draw.line((x - 9, y + 9, x + 9, y - 9),
                  fill=(255, 55, 55, 255), width=4)
        draw.text((x + 11, y - 10), label, font=tiny_font,
                  fill=(255, 106, 106, 255), stroke_width=2,
                  stroke_fill=(8, 10, 14, 255))

    for current in data.get("current_instances", []):
        point = current.get("screen_position")
        if not point or not current.get("failed"):
            continue
        label = f"FAILED #{current['instance_index']}"
        failed_marker(point, full_rect, (0.0, 0.0, 1.0, 1.0), label)
        if bx0 <= point[0] <= bx1 and by0 <= point[1] <= by1:
            failed_marker(point, zoom_rect, screen_bounds, label)

    for region in data.get("regions", []):
        centroid = region.get("screen_centroid")
        if centroid is None:
            continue
        x, y = screen_project(centroid, zoom_rect, screen_bounds)
        label = (f"{region['region_id']}  cells={region['cell_count']}  "
                 f"capacity≈{region.get('estimated_capacity', 0)}")
        bbox = draw.textbbox((0, 0), label, font=label_font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = min(max(x, zoom_rect[0] + tw / 2 + 8),
                zoom_rect[2] - tw / 2 - 8)
        label_y = min(max(y - 38, zoom_rect[1] + 5),
                      zoom_rect[3] - th - 10)
        draw.rounded_rectangle((x - tw / 2 - 7, label_y - 4,
                                x + tw / 2 + 7, label_y + th + 4),
                               radius=5, fill=(20, 82, 46, 240),
                               outline=(105, 255, 158, 255), width=2)
        draw.text((x - tw / 2, label_y), label, font=label_font,
                  fill=(239, 255, 244, 255))
        draw.line((x, label_y + th + 5, x, y),
                  fill=(105, 255, 158, 230), width=2)
    cell_by_id = {str(cell["cell_id"]): cell
                  for cell in data.get("cells", [])}
    for cell_id, selected in selected_by_cell.items():
        cell = cell_by_id.get(cell_id)
        if cell is None:
            continue
        cx, cy = map(float, cell["screen_position"])
        x0, y1 = screen_project(
            (cx - step_x / 2, cy - step_y / 2), zoom_rect, screen_bounds)
        x1, y0 = screen_project(
            (cx + step_x / 2, cy + step_y / 2), zoom_rect, screen_bounds)
        draw.rectangle((x0 - 2, y0 - 2, x1 + 2, y1 + 2),
                       outline=(255, 255, 255, 255), width=4)
        draw.text((x0 + 2, y0 + 1),
                  f"#{selected.get('instance_index', '?')}",
                  font=tiny_font, fill=(255, 255, 255, 255),
                  stroke_width=2, stroke_fill=(4, 6, 8, 255))
    draw.text((zoom_rect[0], zoom_rect[3] + 9),
              f"x={bx0:.3f} → {bx1:.3f}", font=small_font,
              fill=(177, 188, 202, 255))
    draw.text((zoom_rect[2] - 178, zoom_rect[3] + 9),
              f"y={by0:.3f} → {by1:.3f} ↑", font=small_font,
              fill=(177, 188, 202, 255))

    # Filled world-space cell polygons, derived from shared corner raycasts.
    camera = data.get("camera") or {}
    camera_world = camera.get("world_position")
    frustum = camera.get("frustum_world_xy") or []
    if camera_world and len(frustum) >= 2:
        camera_px = world_project(camera_world)
        for index, point in enumerate(frustum):
            next_point = frustum[(index + 1) % len(frustum)]
            draw.polygon((camera_px, world_project(point),
                          world_project(next_point)),
                         fill=(99, 167, 255, 14))

    status_order = {"no_hit": 0, "no_water_voxel": 0, "surface": 1,
                    "terrain": 2, "boundary": 2,
                    "footprint": 3, "slope": 4, "obstacle": 5,
                    "feasible": 6}
    polygon_cells = [cell for cell in data.get("cells", [])
                     if cell.get("world_xy_polygon")]
    for cell in sorted(polygon_cells, key=lambda row: status_order.get(
            row.get("status"), 0)):
        polygon = [world_project(point) for point in cell["world_xy_polygon"]]
        color = ((105, 112, 123, 235) if cell["cell_id"] in excluded_ids
                 else (78, 91, 120, 235)
                 if cell["cell_id"] in reserved_ids
                 else STATUS_COLORS.get(
                     cell.get("status"), (160, 160, 160, 210)))
        draw.polygon(polygon, fill=(*color[:3], 190),
                     outline=(11, 15, 20, 70))

    if camera_world and frustum:
        for point in frustum:
            draw.line((world_project(camera_world), world_project(point)),
                      fill=(153, 204, 255, 210), width=2)
        outline = [world_project(point) for point in frustum]
        draw.line(outline + [outline[0]], fill=(153, 204, 255, 210), width=2)

    # Existing scene objects remain explicit boxes rather than visual clutter.
    for obstacle_index, obstacle in enumerate(data.get("obstacles", [])):
        bounds = obstacle.get("world_xy_bounds")
        if not bounds:
            continue
        x0, y1 = world_project((bounds[0], bounds[1]))
        x1, y0 = world_project((bounds[2], bounds[3]))
        draw.rectangle((x0, y0, x1, y1), fill=(142, 149, 159, 42),
                       outline=(225, 229, 235, 220), width=2)
        if obstacle_index < 40 and max(abs(x1 - x0), abs(y1 - y0)) >= 7:
            label = str(obstacle.get("label") or "context")[:22]
            draw.text((x0 + 3, y0 + 2), label, font=tiny_font,
                      fill=(239, 242, 247, 230))

    half_x, half_y = map(float, data.get("footprint_half_extents", [1.0, 1.0]))
    for current in data.get("current_instances", []):
        world = current.get("world_position")
        if not world:
            continue
        x0, y1 = world_project((world[0] - half_x, world[1] - half_y))
        x1, y0 = world_project((world[0] + half_x, world[1] + half_y))
        failed = bool(current.get("failed"))
        color = (255, 78, 78, 255) if failed else (245, 245, 245, 230)
        draw.rectangle((x0, y0, x1, y1), outline=(5, 8, 10, 255), width=6)
        draw.rectangle((x0, y0, x1, y1), outline=color, width=3)
        draw.text((x0 + 3, y0 + 3),
                  f"{'FAILED' if failed else 'FIXED'} #{current['instance_index']}",
                  font=tiny_font, fill=color, stroke_width=1,
                  stroke_fill=(7, 9, 12, 255))

    for region in data.get("regions", []):
        centroid = region.get("world_xy_centroid")
        if centroid is None:
            continue
        x, y = world_project(centroid)
        label = f"{region['region_id']}  cap≈{region.get('estimated_capacity', 0)}"
        bbox = draw.textbbox((0, 0), label, font=label_font, stroke_width=1)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        rect = (x - tw / 2 - 7, y - th / 2 - 4,
                x + tw / 2 + 7, y + th / 2 + 4)
        draw.rounded_rectangle(rect, radius=5, fill=(23, 91, 52, 238),
                               outline=(106, 255, 159, 255), width=2)
        draw.text((x - tw / 2, y - th / 2), label, font=label_font,
                  fill=(238, 255, 244, 255), stroke_width=1,
                  stroke_fill=(0, 0, 0, 180))

    for cell_id, selected in selected_by_cell.items():
        cell = cell_by_id.get(cell_id)
        if cell is None or not cell.get("world_xy_polygon"):
            continue
        polygon = [world_project(point) for point in cell["world_xy_polygon"]]
        draw.line(polygon + [polygon[0]], fill=(255, 255, 255, 255), width=4)
        world = cell.get("world_position")
        if world:
            x, y = world_project(world)
            draw.ellipse((x - 8, y - 8, x + 8, y + 8),
                         fill=(10, 14, 18, 255),
                         outline=(255, 255, 255, 255), width=3)
            draw.text((x + 10, y - 8),
                      (f"SELECTED #{selected.get('instance_index', '?')}"
                       + (f"  depth={float(selected['candidate_depth_m']):.1f}m"
                          if selected.get("candidate_depth_m") is not None else "")),
                      font=tiny_font, fill=(255, 255, 255, 255),
                      stroke_width=2, stroke_fill=(5, 7, 10, 255))

    if camera_world:
        x, y = world_project(camera_world)
        draw.ellipse((x - 9, y - 9, x + 9, y + 9),
                     fill=(255, 255, 255, 255),
                     outline=(18, 21, 27, 255), width=3)
        draw.text((x + 12, y - 9), "CAMERA", font=label_font,
                  fill=(255, 255, 255, 255))

    axis_x, axis_y = world_right - 120, world_bottom - 36
    draw.line((axis_x, axis_y, axis_x + 55, axis_y),
              fill=(255, 95, 95, 255), width=4)
    draw.line((axis_x, axis_y, axis_x, axis_y - 55),
              fill=(95, 235, 135, 255), width=4)
    draw.text((axis_x + 61, axis_y - 9), "+X", font=small_font,
              fill=(255, 140, 140, 255))
    draw.text((axis_x - 12, axis_y - 73), "+Y", font=small_font,
              fill=(140, 255, 175, 255))
    draw.text((right_panel[0] + 18, right_panel[3] - 27),
              "Filled polygons = actual screen-cell corner rays in world XY",
              font=small_font, fill=(177, 187, 201, 255))

    draw.rectangle((0, height - footer, width, height),
                   fill=(13, 16, 21, 255))
    legend = [
        ("FEASIBLE", "feasible"), ("WRONG SURFACE", "surface"),
        ("SLOPE", "slope"), ("FOOTPRINT", "footprint"),
        ("BOUNDARY", "boundary"), ("OBJECT", "obstacle"),
        ("SEABED", "terrain"), ("NO 3D WATER", "no_water_voxel"),
    ]
    legend_x = 24
    for label, status in legend:
        color = STATUS_COLORS[status]
        draw.rectangle((legend_x, height - 73, legend_x + 19, height - 54),
                       fill=color)
        draw.text((legend_x + 27, height - 75), label, font=small_font,
                  fill=(232, 236, 243, 255))
        legend_x += draw.textbbox((0, 0), label, font=small_font)[2] + 58
    counts = Counter(cell.get("status") for cell in data.get("cells", []))
    summary = "   ".join(f"{key}={counts.get(key, 0)}" for key in STATUS_COLORS)
    draw.text((24, height - 36), summary, font=small_font,
              fill=(190, 198, 211, 255))
    draw.text((width - 675, height - 36),
              "Green cells alone satisfy surface + slope + footprint + boundary",
              font=small_font, fill=(177, 190, 180, 255))
    if excluded_ids or reserved_ids or selected_rows:
        draw.text((width - 675, height - 72),
                  f"gray excluded={len(excluded_ids)}   "
                  f"slate other-edit reserved={len(reserved_ids)}   "
                  f"white selected={len(selected_rows)}",
                  font=small_font, fill=(211, 216, 224, 255))

    output.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(output, "PNG")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--map", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = json.loads(args.map.read_text(encoding="utf-8"))
    render_region_map(data, args.output)
    print(f"PLACEMENT_REGION_MAP_RENDERED → {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
