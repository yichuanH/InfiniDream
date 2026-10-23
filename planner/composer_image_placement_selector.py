#!/usr/bin/env python
"""Let Composer choose indoor placement points from the original scene render.

The Agent-facing contract is intentionally semantic: it sees the original
camera render and decides where each requested object should go.  Deterministic
code then snaps those approximate screen points to the nearest machine-feasible
visible support point when a region map is available.  The Agent never chooses
grid cells.
"""

from __future__ import annotations

import argparse
import base64
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import sys

try:
    from .composer import _env
    from .placement_critic import extract_json
except ImportError:  # direct script execution keeps planner/ on sys.path
    from composer import _env
    from placement_critic import extract_json

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
STAGE_EXEC = ROOT / "stage_exec"
if str(STAGE_EXEC) not in sys.path:
    sys.path.insert(0, str(STAGE_EXEC))

import placement_contract as plc  # noqa: E402
import surface_registry as sr  # noqa: E402
from placement_region_contract import (  # noqa: E402
    all_map_managed_instances,
)

try:
    from semantic_support_patches import build_support_patches
except ImportError:  # pragma: no cover - only possible in unusual packaging
    build_support_patches = None


SYSTEM = """You are the image-guided placement mode of a 3D scene Composer.
You look at the ORIGINAL scene render and decide where the requested new
objects belong semantically. You are not choosing tiny grid cells.

Return approximate normalized screen coordinates for the intended contact point
of every requested object instance. Coordinates use bottom-left origin:
x increases rightward, y increases upward, and every point must stay inside the
given screen_bounds.

Use the image for semantics and composition. Use machine_feasible_support_summary
only as deterministic geometry hints: support_patch_id names a whole visible
support plane/patch, not a grid cell. If a patch id is clearly appropriate, copy
it; otherwise leave support_patch_id null and choose the visible image point.
Do not blindly prefer the floor: choose the support kind that is natural for
the object and scene, such as tabletop/countertop/shelf for display objects,
bed_top for soft toys, seat for cushions, and floor/back_to_wall for furniture
or large objects.

placement_mode tells deterministic code how to use the chosen support:
center = normal object-on-surface placement.
edge = near the edge of a table/counter/shelf/seat.
back_to_wall = floor/seat/table point close to the rear wall or backrest.
row = use when multiple instances should line up, such as chairs along a wall.
wall_mount = only for objects meant to attach to a vertical wall.

For several related instances, produce a coherent layout in one reply: preserve
spacing, line up rows when requested, and do not stack points unless overlap is
intentional. Do not claim success; deterministic raycast and Machine Review are
the authority.

Return exactly one JSON object:
{"decision":"place_from_image","placements":[
  {"edit_id":"...","instance_index":0,
   "screen_position":[0.52,0.34],
   "placement_mode":"center",
   "support_patch_id":null,
   "support_kind":"seat",
   "reason":"..."}
]}"""


def _sha256_json(value: dict) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _image_block(path: Path) -> dict:
    media_type = "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(path.read_bytes()).decode("ascii"),
        },
    }


def load_region_maps(index_path: Path | None) -> dict[str, dict]:
    if index_path is None or not index_path.is_file():
        return {}
    index = json.loads(index_path.read_text(encoding="utf-8"))
    scene_name = str(index.get("scene_name") or index.get("scene") or "")
    maps = {}
    for row in index.get("maps", []):
        edit_id = str(row.get("edit_id") or "")
        map_json = row.get("map_json")
        if not edit_id or not map_json:
            continue
        data = json.loads(
            (index_path.parent / map_json).read_text(encoding="utf-8"))
        if scene_name and not data.get("scene_name"):
            data["scene_name"] = scene_name
        for cell in data.get("cells", []):
            if cell.get("surface_types"):
                continue
            inferred = sr.types_for_mesh(
                scene_name, str(cell.get("hit_surface") or ""))
            if inferred:
                cell["surface_types"] = inferred
        maps[edit_id] = data
    return maps


def _target_edits(plan: dict) -> dict[str, dict]:
    expected = all_map_managed_instances(plan)
    by_id = {
        str(edit.get("code_from") or ""): edit
        for edit in plan.get("hero_edits", [])
        if edit.get("op") == "add" and edit.get("code_from")
    }
    return {
        edit_id: by_id[edit_id]
        for edit_id in sorted(expected)
        if edit_id in by_id
    }


def _screen_centroid(points: list[list[float]] | None) -> list[float] | None:
    if not points:
        return None
    valid = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        try:
            x, y = float(point[0]), float(point[1])
        except (TypeError, ValueError):
            continue
        if math.isfinite(x) and math.isfinite(y):
            valid.append((x, y))
    if not valid:
        return None
    return [
        sum(point[0] for point in valid) / len(valid),
        sum(point[1] for point in valid) / len(valid),
    ]


def _compact_support_summary(map_data: dict | None) -> dict | None:
    if not map_data:
        return None
    feasible = [
        cell for cell in map_data.get("cells", [])
        if cell.get("status") == "feasible"
    ]
    by_kind: dict[str, int] = {}
    for cell in feasible:
        for surface_type in cell.get("surface_types") or []:
            key = str(surface_type)
            by_kind[key] = by_kind.get(key, 0) + 1
    patches = []
    if build_support_patches is not None:
        for patch in build_support_patches(map_data)[:16]:
            patch_id = str(
                patch.get("patch_id") or patch.get("support_plane_id") or "")
            if not patch_id:
                continue
            patches.append({
                "support_patch_id": patch_id,
                "support_kind": patch.get("support_kind"),
                "patch_label": patch.get("patch_label"),
                "surface_types": patch.get("surface_types"),
                "placement_modes": patch.get("placement_modes"),
                "screen_bounds": patch.get("screen_bounds"),
                "screen_centroid": _screen_centroid(
                    patch.get("screen_polygon")),
                "visible_cell_count": patch.get("visible_cell_count"),
                "usable_cell_count": patch.get("usable_cell_count"),
            })
    return {
        "screen_bounds": map_data.get("screen_bounds"),
        "feasible_cell_count": len(feasible),
        "support_kind_counts": by_kind,
        "support_patches": patches,
    }


def build_payload(plan: dict, manifest: dict | None,
                  maps: dict[str, dict]) -> dict:
    edits = []
    expected = all_map_managed_instances(plan)
    for edit_id, edit in _target_edits(plan).items():
        count = int(edit.get("n", 0))
        spec = plc.normalize_placement_spec(
            edit.get("placement_spec"), count=count,
            anchoring=edit.get("anchoring"))
        edits.append({
            "edit_id": edit_id,
            "factory": edit.get("factory"),
            "count": count,
            "instance_indices": sorted(expected.get(edit_id, set())),
            "anchoring": edit.get("anchoring"),
            "semantic_target": spec.get("semantic_target"),
            "arrangement": spec.get("arrangement"),
            "row_axis": spec.get("row_axis"),
            "current_placement_mode": spec.get("placement_mode"),
            "allowed_placement_modes": sorted(plc.PLACEMENT_MODES),
            "screen_bounds": spec.get("screen_bounds"),
            "surface_policy": spec.get("surface_policy"),
            "proxy_dimensions_m": spec.get("proxy_dimensions_m"),
            "current_screen_positions": edit.get("screen_positions"),
            "machine_feasible_support_summary": _compact_support_summary(
                maps.get(edit_id)),
        })
    surface_inventory = []
    if manifest:
        surface_inventory = [
            {
                "type": row.get("type"),
                "mesh_count": len(row.get("mesh_names") or []),
                "example_mesh_names": list(row.get("mesh_names") or [])[:5],
            }
            for row in manifest.get("available_surface_types", [])
        ]
    return {
        "contract_version": 1,
        "task": "choose semantic image locations; code will raycast/snap",
        "requested_add_edits": edits,
        "available_surface_types": surface_inventory,
    }


def _known_instances(plan: dict) -> set[tuple[str, int]]:
    expected = all_map_managed_instances(plan)
    return {
        (edit_id, index)
        for edit_id, indices in expected.items()
        for index in indices
    }


def normalize_image_selection(selection: dict, plan: dict) -> list[dict]:
    if selection.get("decision") != "place_from_image":
        raise ValueError("selection must use decision='place_from_image'")
    rows = selection.get("placements")
    if not isinstance(rows, list):
        raise ValueError("placements must be a list")
    edits = _target_edits(plan)
    required = _known_instances(plan)
    seen = set()
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each placement must be an object")
        edit_id = str(row.get("edit_id") or "")
        if row.get("instance_index") is None:
            raise ValueError("placement is missing instance_index")
        index = int(row["instance_index"])
        key = (edit_id, index)
        if key not in required:
            raise ValueError(f"unknown or unowned placement instance {key}")
        if key in seen:
            raise ValueError(f"duplicate placement instance {key}")
        seen.add(key)
        edit = edits[edit_id]
        count = int(edit.get("n", 0))
        spec = plc.normalize_placement_spec(
            edit.get("placement_spec"), count=count,
            anchoring=edit.get("anchoring"))
        point = row.get("screen_position")
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError(f"{key} screen_position must be [x,y]")
        x, y = map(float, point)
        plc.normalize_screen_positions([[x, y]], spec=spec, count=1)
        mode = str(row.get("placement_mode") or spec.get("placement_mode"))
        if mode not in plc.PLACEMENT_MODES:
            raise ValueError(
                f"{key} invalid placement_mode {mode!r}; "
                f"use {sorted(plc.PLACEMENT_MODES)}")
        if spec.get("placement_mode") == "back_to_wall" and mode == "row":
            mode = "back_to_wall"
        normalized.append({
            "edit_id": edit_id,
            "instance_index": index,
            "screen_position": [x, y],
            "placement_mode": mode,
            "support_patch_id": (
                None if row.get("support_patch_id") in ("", None)
                else str(row.get("support_patch_id"))),
            "support_kind": (
                None if row.get("support_kind") in ("", None)
                else str(row.get("support_kind")).casefold()),
            "reason": str(row.get("reason") or ""),
        })
    if seen != required:
        raise ValueError(
            "placements must cover every map-managed add instance; "
            f"missing={sorted(required - seen)} extra={sorted(seen - required)}")
    return sorted(normalized, key=lambda row: (
        row["edit_id"], row["instance_index"]))


def image_selection_from_plan_screen_positions(plan: dict) -> list[dict]:
    rows = []
    expected = all_map_managed_instances(plan)
    for edit_id, edit in _target_edits(plan).items():
        count = int(edit.get("n", 0))
        spec = plc.normalize_placement_spec(
            edit.get("placement_spec"), count=count,
            anchoring=edit.get("anchoring"))
        points = plc.normalize_screen_positions(
            edit.get("screen_positions"), spec=spec, count=count)
        intent_by_index = {
            int(row.get("instance_index")): row
            for row in edit.get("composer_image_placement_intent") or []
            if isinstance(row, dict)
            and row.get("instance_index") is not None
        }
        for index in sorted(expected.get(edit_id, set())):
            intent = intent_by_index.get(index, {})
            rows.append({
                "edit_id": edit_id,
                "instance_index": index,
                "screen_position": points[index],
                "placement_mode": (
                    intent.get("placement_mode")
                    or spec.get("placement_mode")
                    or "center"),
                "support_patch_id": intent.get("support_patch_id"),
                "support_kind": intent.get("support_kind"),
                "reason": (
                    "resnap Composer-revised screen_position after "
                    "Machine Review"),
            })
    if not rows:
        return []
    return normalize_image_selection({
        "decision": "place_from_image",
        "placements": rows,
    }, plan)


def _distance(left, right) -> float:
    return math.dist(list(map(float, left[:2])), list(map(float, right[:2])))


def _cell_world_position(cell: dict) -> list[float] | None:
    raw = cell.get("candidate_world_position") or cell.get("world_position")
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        return None
    try:
        values = [float(value) for value in raw]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    return values


def _normalize_yaw(yaw: float) -> float:
    value = float(yaw) % math.tau
    return 0.0 if math.isclose(value, math.tau, abs_tol=1e-8) else value


def _angle_distance(left: float, right: float) -> float:
    return abs((float(left) - float(right) + math.pi) % math.tau - math.pi)


def _camera_facing_yaw(cell: dict, map_data: dict) -> float | None:
    world = _cell_world_position(cell)
    camera = (map_data.get("camera") or {}).get("world_position")
    if (world is None or not isinstance(camera, (list, tuple))
            or len(camera) < 2):
        return None
    try:
        dx = float(camera[0]) - float(world[0])
        dy = float(camera[1]) - float(world[1])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(dx) or not math.isfinite(dy):
        return None
    if math.hypot(dx, dy) <= 1e-8:
        return None
    # Placeholder-local +X is the semantic heading used by generated assets.
    return _normalize_yaw(math.atan2(dy, dx))


def _best_feasible_yaw_for_camera(cell: dict, map_data: dict) -> float | None:
    candidates = []
    for value in cell.get("feasible_yaws") or []:
        try:
            yaw = _normalize_yaw(float(value))
        except (TypeError, ValueError):
            continue
        if math.isfinite(yaw):
            candidates.append(yaw)
    if not candidates:
        return None
    desired = _camera_facing_yaw(cell, map_data)
    if desired is None:
        return candidates[0]
    return min(candidates, key=lambda yaw: (
        _angle_distance(yaw, desired),
        yaw,
    ))


def _map_half_extents(map_data: dict) -> tuple[float, float]:
    raw = map_data.get("proxy_half_extents") or map_data.get(
        "footprint_half_extents")
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        try:
            return (
                max(0.05, float(raw[0])),
                max(0.05, float(raw[1])),
            )
        except (TypeError, ValueError):
            pass
    dimensions = map_data.get("proxy_dimensions_m")
    if isinstance(dimensions, (list, tuple)) and len(dimensions) >= 2:
        try:
            return (
                max(0.05, float(dimensions[0]) / 2.0),
                max(0.05, float(dimensions[1]) / 2.0),
            )
        except (TypeError, ValueError):
            pass
    return (0.5, 0.5)


def _overlaps_occupied(cell: dict, map_data: dict,
                       occupied: list[dict]) -> bool:
    return _overlap_penalty(cell, map_data, occupied) > 0.0


def _overlap_penalty(cell: dict, map_data: dict,
                     occupied: list[dict]) -> float:
    world = _cell_world_position(cell)
    if world is None:
        return 0.0
    half_x, half_y = _map_half_extents(map_data)
    penalty = 0.0
    for other in occupied:
        other_world = other.get("world_position")
        other_half = other.get("half_extents") or (0.5, 0.5)
        if not isinstance(other_world, (list, tuple)) or len(other_world) < 2:
            continue
        pad = 0.15
        overlap_x = (
            half_x + float(other_half[0]) + pad
            - abs(float(world[0]) - float(other_world[0])))
        overlap_y = (
            half_y + float(other_half[1]) + pad
            - abs(float(world[1]) - float(other_world[1])))
        if overlap_x > 0.0 and overlap_y > 0.0:
            penalty += overlap_x * overlap_y
    return penalty


def _snap_row_to_map(row: dict, map_data: dict,
                     used_cells: set[str],
                     occupied: list[dict]) -> tuple[dict, dict | None]:
    feasible = [
        cell for cell in map_data.get("cells", [])
        if cell.get("status") == "feasible"
        and cell.get("screen_position") is not None
    ]
    if not feasible:
        return row, None
    preferred = list(map(float, row["screen_position"]))
    patch_id = str(row.get("support_patch_id") or "")
    support_kind = str(row.get("support_kind") or "").casefold()
    filtered = feasible
    filter_notes = []
    if patch_id:
        by_patch = [
            cell for cell in filtered
            if str(cell.get("support_plane_id") or cell.get("region_id") or "")
            == patch_id
        ]
        if by_patch:
            filtered = by_patch
            filter_notes.append(f"support_patch_id={patch_id}")
    if support_kind:
        by_kind = [
            cell for cell in filtered
            if support_kind in {str(value).casefold()
                                for value in cell.get("surface_types") or []}
        ]
        if by_kind:
            filtered = by_kind
            filter_notes.append(f"support_kind={support_kind}")
    unused = [
        cell for cell in filtered
        if str(cell.get("cell_id") or "") not in used_cells
    ]
    if unused:
        filtered = unused
        filter_notes.append("unused_global_cell")
    non_overlapping = [
        cell for cell in filtered
        if not _overlaps_occupied(cell, map_data, occupied)
    ]
    if non_overlapping:
        filtered = non_overlapping
        filter_notes.append("cross_edit_non_overlapping")

    mode = str(row.get("placement_mode") or "center")
    def overlap_penalty(cell) -> float:
        return _overlap_penalty(cell, map_data, occupied)

    def clearance(cell) -> int:
        try:
            return int(cell.get("clearance_steps") or 0)
        except (TypeError, ValueError):
            return 0

    def point(cell) -> list[float]:
        return list(map(float, cell["screen_position"]))

    if mode == "back_to_wall":
        chosen = min(filtered, key=lambda cell: (
            overlap_penalty(cell),
            abs(point(cell)[0] - preferred[0]) + 0.35 * abs(point(cell)[1] - preferred[1])
            - 0.18 * point(cell)[1],
            -clearance(cell),
            str(cell.get("cell_id") or ""),
        ))
    elif mode == "edge":
        chosen = min(filtered, key=lambda cell: (
            overlap_penalty(cell),
            _distance(point(cell), preferred),
            clearance(cell),
            str(cell.get("cell_id") or ""),
        ))
    else:
        chosen = min(filtered, key=lambda cell: (
            overlap_penalty(cell),
            _distance(point(cell), preferred),
            -clearance(cell),
            str(cell.get("cell_id") or ""),
        ))

    cell_id = str(chosen.get("cell_id") or "")
    if cell_id:
        used_cells.add(cell_id)
    world = _cell_world_position(chosen)
    if world is not None:
        occupied.append({
            "world_position": world,
            "half_extents": _map_half_extents(map_data),
            "cell_id": cell_id,
        })
    snapped = dict(row)
    snapped["screen_position"] = list(map(float, chosen["screen_position"]))
    snapped["snapped_from_screen_position"] = preferred
    snapped["snap_source"] = "nearest_machine_feasible_support_point"
    snapped["cell_id"] = chosen.get("cell_id")
    snapped["semantic_cell_id"] = chosen.get("semantic_cell_id")
    snapped["region_id"] = chosen.get("region_id")
    snapped["support_plane_id"] = chosen.get("support_plane_id")
    snapped["support_patch_id"] = (
        row.get("support_patch_id")
        or chosen.get("support_plane_id")
        or chosen.get("region_id"))
    snapped["surface_types"] = chosen.get("surface_types")
    snapped["hit_surface"] = chosen.get("hit_surface")
    snapped["world_position"] = chosen.get("world_position")
    snapped["candidate_world_position"] = chosen.get("candidate_world_position")
    snapped["surface_normal"] = chosen.get("surface_normal")
    snapped["slope_deg"] = chosen.get("slope_deg")
    snapped["clearance_steps"] = chosen.get("clearance_steps")
    snapped["feasible_yaws"] = chosen.get("feasible_yaws")
    selected_yaw = _best_feasible_yaw_for_camera(chosen, map_data)
    if selected_yaw is not None:
        snapped["placement_yaw"] = round(float(selected_yaw), 8)
        snapped["placement_yaw_degrees"] = round(
            math.degrees(float(selected_yaw)), 3)
    snap_note = {
        "edit_id": row["edit_id"],
        "instance_index": row["instance_index"],
        "requested_screen_position": preferred,
        "snapped_screen_position": snapped["screen_position"],
        "cell_id": snapped.get("cell_id"),
        "region_id": snapped.get("region_id"),
        "support_plane_id": snapped.get("support_plane_id"),
        "placement_mode": mode,
        "placement_yaw": snapped.get("placement_yaw"),
        "placement_yaw_degrees": snapped.get("placement_yaw_degrees"),
        "filters": filter_notes,
    }
    return snapped, snap_note


def snap_selection_to_maps(rows: list[dict],
                           maps: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    if not maps:
        return rows, []
    snapped = []
    notes = []
    used_cells: set[str] = set()
    occupied: list[dict] = []
    for row in rows:
        edit_id = str(row["edit_id"])
        mapped, note = _snap_row_to_map(
            row, maps.get(edit_id) or {}, used_cells, occupied)
        snapped.append(mapped)
        if note is not None:
            notes.append(note)
    return snapped, notes


def _explicit_world_position(row: dict) -> list[float] | None:
    raw = row.get("candidate_world_position") or row.get("world_position")
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        return None
    try:
        values = [float(value) for value in raw]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    return values


def _explicit_support_record(row: dict) -> dict:
    return {
        "surface": row.get("hit_surface"),
        "hit_surface": row.get("hit_surface"),
        "surface_normal": row.get("surface_normal"),
        "slope_deg": row.get("slope_deg"),
        "semantic_cell_id": row.get("semantic_cell_id"),
        "cell_id": row.get("cell_id"),
        "region_id": row.get("region_id"),
        "support_plane_id": row.get("support_plane_id"),
        "surface_types": row.get("surface_types"),
        "clearance_steps": row.get("clearance_steps"),
    }


def _selected_indoor_surface_policy(spec: dict, rows: list[dict]) -> dict | None:
    selected_types = []
    selected_names = []
    for row in rows:
        for surface_type in row.get("surface_types") or []:
            surface_type = str(surface_type)
            if (surface_type in plc.INDOOR_PLANAR_SURFACE_TYPES
                    and surface_type not in selected_types):
                selected_types.append(surface_type)
        surface_name = row.get("hit_surface")
        if surface_name:
            surface_name = str(surface_name)
            if not any(plc.surface_name_matches(surface_name, existing)
                       for existing in selected_names):
                selected_names.append(surface_name)
    if not selected_types and not selected_names:
        return None
    policy = plc.normalize_surface_policy(spec.get("surface_policy"))
    policy["required_surface_types"] = selected_types
    policy["required_surface_names"] = selected_names
    required_types = set(selected_types)
    policy["forbidden_surface_types"] = [
        str(value)
        for value in policy.get("forbidden_surface_types") or []
        if str(value) not in required_types
    ]
    policy["forbidden_surface_names"] = [
        str(value)
        for value in policy.get("forbidden_surface_names") or []
        if not any(plc.surface_name_matches(value, selected)
                   for selected in selected_names)
    ]
    return plc.harden_indoor_surface_policy(policy)


def apply_image_selection(plan: dict, rows: list[dict]) -> tuple[dict, dict]:
    revised = deepcopy(plan)
    rows_by_edit: dict[str, list[dict]] = {}
    for row in rows:
        rows_by_edit.setdefault(str(row["edit_id"]), []).append(row)

    applied = []
    for edit in revised.get("hero_edits", []):
        if edit.get("op") != "add":
            continue
        edit_id = str(edit.get("code_from") or "")
        rows_for_edit = rows_by_edit.get(edit_id)
        if not rows_for_edit:
            continue
        count = int(edit.get("n", 0))
        spec = plc.normalize_placement_spec(
            edit.get("placement_spec"), count=count,
            anchoring=edit.get("anchoring"))
        current = edit.get("screen_positions")
        if current is None:
            current = plc.initial_screen_positions(spec, count)
        points = plc.normalize_screen_positions(
            current, spec=spec, count=count)
        placement_yaws = edit.get("placement_yaws")
        if not isinstance(placement_yaws, list) or len(placement_yaws) != count:
            placement_yaws = [None] * count
        explicit_positions = [None] * count
        explicit_supports = [{} for _ in range(count)]
        owns_explicit_positions = True
        mode_by_index = {}
        for row in rows_for_edit:
            index = int(row["instance_index"])
            before = deepcopy(points[index])
            points[index] = list(map(float, row["screen_position"]))
            explicit_position = _explicit_world_position(row)
            if explicit_position is None:
                owns_explicit_positions = False
            else:
                explicit_positions[index] = explicit_position
                explicit_supports[index] = _explicit_support_record(row)
            yaw = row.get("placement_yaw")
            if isinstance(yaw, (int, float)):
                placement_yaws[index] = float(yaw)
            else:
                feasible_yaws = [
                    float(value) for value in (row.get("feasible_yaws") or [])
                    if isinstance(value, (int, float))
                ]
                if feasible_yaws:
                    placement_yaws[index] = feasible_yaws[0]
            mode_by_index[index] = row.get("placement_mode")
            applied.append({
                "edit_id": edit_id,
                "instance_index": index,
                "screen_position_before": before,
                "screen_position_after": points[index],
                "placement_mode": row.get("placement_mode"),
                "support_patch_id": row.get("support_patch_id"),
                "support_plane_id": row.get("support_plane_id"),
                "region_id": row.get("region_id"),
                "cell_id": row.get("cell_id"),
                "semantic_cell_id": row.get("semantic_cell_id"),
                "hit_surface": row.get("hit_surface"),
                "surface_types": row.get("surface_types"),
                "surface_normal": row.get("surface_normal"),
                "slope_deg": row.get("slope_deg"),
                "clearance_steps": row.get("clearance_steps"),
                "world_position": row.get("world_position"),
                "candidate_world_position": row.get("candidate_world_position"),
                "placement_yaw": row.get("placement_yaw"),
                "placement_yaw_degrees": row.get("placement_yaw_degrees"),
                "reason": row.get("reason"),
                "snap_source": row.get("snap_source"),
                "snapped_from_screen_position": row.get(
                    "snapped_from_screen_position"),
            })
        edit["screen_positions"] = plc.normalize_screen_positions(
            points, spec=spec, count=count)
        edit["screen_positions_source"] = "composer_image_semantic"
        edit["screen_position_projection"] = "visible_surface"
        selected_policy = _selected_indoor_surface_policy(spec, rows_for_edit)
        if selected_policy is not None:
            edit.setdefault("placement_spec", {})[
                "surface_policy"] = selected_policy
        if owns_explicit_positions and all(
                isinstance(position, list) and len(position) == 3
                for position in explicit_positions):
            edit["positions"] = explicit_positions
            edit["positions_source"] = "composer_image_semantic_map_snap"
            edit["explicit_position_supports"] = explicit_supports
        if any(value is not None for value in placement_yaws):
            edit["placement_yaws"] = placement_yaws
        unique_modes = {
            str(value) for value in mode_by_index.values()
            if value is not None
        }
        if len(unique_modes) == 1:
            edit.setdefault("placement_spec", {})["placement_mode"] = (
                unique_modes.pop())
        edit["composer_image_placement_intent"] = [
            {
                "instance_index": int(row["instance_index"]),
                "placement_mode": row.get("placement_mode"),
                "support_patch_id": row.get("support_patch_id"),
                "support_kind": row.get("support_kind"),
                "reason": row.get("reason"),
                "selected_screen_position": row.get(
                    "snapped_from_screen_position")
                or row.get("screen_position"),
                "executor_screen_position": row.get("screen_position"),
                "executor_yaw_rad": row.get("placement_yaw"),
                "executor_yaw_deg": row.get("placement_yaw_degrees"),
            }
            for row in sorted(rows_for_edit, key=lambda item: item["instance_index"])
        ]
    return revised, {"contract_version": 1, "applied": applied}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--scene-render", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--plan-output", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--map-index", type=Path)
    parser.add_argument("--application", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--from-plan-screen-positions", action="store_true",
                        help="skip API; resnap existing plan screen_positions")
    parser.add_argument("--model", default=os.environ.get(
        "COMPOSER_MODEL", "claude-opus-4-8"))
    args = parser.parse_args()

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    manifest = (
        json.loads(args.manifest.read_text(encoding="utf-8"))
        if args.manifest and args.manifest.is_file() else None)
    maps = load_region_maps(args.map_index)
    before_hash = _sha256_json(plan)
    payload = build_payload(plan, manifest, maps)
    if not payload["requested_add_edits"]:
        result = {
            "contract_version": 1,
            "decision": "noop",
            "reason": "no map-managed add edits need image-guided placement",
            "plan_before_sha256": before_hash,
            "plan_after_sha256": before_hash,
            "raw_response": "",
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        return 0

    normalized = None
    raw = ""
    correction = None
    if args.from_plan_screen_positions:
        normalized = image_selection_from_plan_screen_positions(plan)
        raw = "FROM_PLAN_SCREEN_POSITIONS_NO_API"
    else:
        values = _env()
        key = values.get("CLAUDE_API_KEY")
        if not key:
            raise SystemExit("CLAUDE_API_KEY not found")
        base = (values.get("CLAUDE_BASE_URL") or "").rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]

        import anthropic

        client = anthropic.Anthropic(api_key=key, base_url=base or None)
        for attempt in range(1, 4):
            request_payload = dict(payload)
            if correction:
                request_payload["previous_reply_contract_error"] = correction
                request_payload["instruction"] = (
                    "Return corrected placements covering every requested "
                    "instance exactly once and staying inside each screen_bounds.")
            response = client.messages.create(
                model=args.model,
                max_tokens=2200,
                system=SYSTEM,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": json.dumps(
                        request_payload, ensure_ascii=False)},
                    {"type": "text", "text": "ORIGINAL SCENE RENDER"},
                    _image_block(args.scene_render),
                ]}],
            )
            raw = "".join(block.text for block in response.content
                          if block.type == "text")
            try:
                decision = extract_json(
                    raw, required_keys=("decision", "placements"),
                    what="image-guided placement")
                normalized = normalize_image_selection(decision, plan)
                if attempt > 1:
                    print(
                        "[composer-image-placement] corrected contract "
                        f"on attempt {attempt}")
                break
            except (TypeError, ValueError) as error:
                correction = str(error)
                print(
                    "[composer-image-placement] invalid contract "
                    f"attempt {attempt}/3: {error}")
    if normalized is None:
        raise ValueError(
            "Composer image placement failed deterministic contract after "
            f"3 attempts: {correction}")

    snapped, snap_notes = snap_selection_to_maps(normalized, maps)
    revised, application = apply_image_selection(plan, snapped)
    application["selection_policy"] = (
        "composer_image_semantic_with_machine_feasible_snap"
        if snap_notes else "composer_image_semantic_screen_raycast")
    application["snap_notes"] = snap_notes

    after_hash = _sha256_json(revised)
    result = {
        "contract_version": 1,
        "decision": "place_from_image",
        "selection_policy": application["selection_policy"],
        "placements": snapped,
        "snap_notes": snap_notes,
        "plan_before_sha256": before_hash,
        "plan_after_sha256": after_hash,
        "raw_response": raw,
        "payload": payload,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    if args.application is not None:
        args.application.parent.mkdir(parents=True, exist_ok=True)
        args.application.write_text(
            json.dumps(application, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    if args.plan_output is not None:
        args.plan_output.parent.mkdir(parents=True, exist_ok=True)
        args.plan_output.write_text(
            json.dumps(revised, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    if args.apply:
        args.plan.write_text(
            json.dumps(revised, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    print(
        "COMPOSER_IMAGE_PLACEMENT_APPLIED "
        f"{len(application['applied'])} instance(s) -> {args.plan}")
    for row in application["applied"]:
        print(
            f"  {row['edit_id']}#{row['instance_index']} "
            f"{row['screen_position_after']} "
            f"mode={row.get('placement_mode')} "
            f"patch={row.get('support_patch_id')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
