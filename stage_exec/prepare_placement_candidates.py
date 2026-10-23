#!/usr/bin/env python
"""Build a temporary Composer plan containing labeled placement candidates.

The temporary plan is only for a Step-8 preview.  It keeps every non-target
instance at its exact current screen coordinate and replaces one target
instance with several deterministic alternatives.  A vision critic later
selects a candidate id and applies only that one coordinate to the real plan.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import surface_registry as sr
from semantic_support_patches import build_support_patches, patch_anchor_cell_id


def explicit_support_record(row: dict) -> dict:
    """Return apply_plan support metadata aligned with an explicit position."""
    return {
        "surface": row.get("hit_surface"),
        "surface_normal": row.get("surface_normal"),
        "slope_deg": row.get("slope_deg"),
        "semantic_cell_id": row.get("semantic_cell_id"),
        "cell_id": row.get("cell_id"),
        "region_id": row.get("region_id"),
        "support_plane_id": row.get("support_plane_id"),
        "surface_types": row.get("surface_types"),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--edit-id", required=True)
    parser.add_argument("--instance-index", type=int, required=True)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--batch", type=int, default=0)
    parser.add_argument("--points-file", type=Path)
    parser.add_argument("--semantic-map-index", type=Path)
    parser.add_argument("--output-plan", type=Path, required=True)
    parser.add_argument("--output-candidates", type=Path, required=True)
    return parser.parse_args()


def candidate_points(bounds, *, count: int, batch: int = 0,
                     anchoring: str | None = None) -> list[list[float]]:
    """Return a stable low-discrepancy batch without repeating prior batches."""
    if not 2 <= int(count) <= 300:
        raise ValueError("candidate count must be in 2..300")
    xmin, ymin, xmax, ymax = map(float, bounds)
    width, height = xmax - xmin, ymax - ymin
    # Ground/support candidates live in the lower visual band.  This avoids
    # spending most candidates in empty sky while making no ice/water decision.
    if anchoring not in {"airborne"}:
        ymax = min(ymax, ymin + min(height, 0.52))
        height = ymax - ymin
    def van_der_corput(index: int, base: int) -> float:
        value, denominator = 0.0, 1.0
        while index:
            index, remainder = divmod(index, base)
            denominator *= base
            value += remainder / denominator
        return value

    points = []
    for index in range(int(count)):
        sequence_index = int(batch) * int(count) + index + 1
        x_fraction = van_der_corput(sequence_index, 2)
        y_fraction = van_der_corput(sequence_index, 3)
        x = xmin + (0.08 + 0.84 * x_fraction) * width
        y = ymin + (0.10 + 0.80 * y_fraction) * height
        points.append([round(x, 6), round(y, 6)])
    return points


def _patch_variants(patch: dict, desired_mode: str | None) -> list[str]:
    modes = list(patch.get("placement_modes") or ["center"])
    if desired_mode and desired_mode in modes:
        ordered = [desired_mode]
    else:
        ordered = list(modes)
    return ordered


def semantic_candidate_rows(*, map_index: Path, edit_id: str, count: int,
                            batch: int, surface_policy: dict,
                            semantic_target: str,
                            placement_mode: str | None = None) -> list[dict]:
    """Rank patch-level feasible support regions from the semantic map."""
    index = json.loads(map_index.read_text(encoding="utf-8"))
    scene_name = str(index.get("scene_name") or index.get("scene") or "")
    support_grid = index.get("semantic_support_grid")
    map_row = (
        dict(support_grid, edit_id="__semantic_support_grid")
        if isinstance(support_grid, dict) and support_grid.get("map_json")
        else next(
            (row for row in index.get("maps", [])
             if str(row.get("edit_id") or "") == str(edit_id)),
            None,
        )
    )
    if map_row is None:
        raise ValueError(f"region map index has no map for {edit_id!r}")
    map_data = json.loads(
        (map_index.parent / map_row["map_json"]).read_text(encoding="utf-8"))
    if scene_name and not map_data.get("scene_name"):
        map_data["scene_name"] = scene_name
    for cell in map_data.get("cells", []):
        if cell.get("surface_types"):
            continue
        inferred = sr.types_for_mesh(scene_name, str(cell.get("hit_surface") or ""))
        if inferred:
            cell["surface_types"] = inferred
    required_types = {
        str(value) for value in surface_policy.get("required_surface_types") or []
    }
    cells = {
        str(cell.get("cell_id")): cell
        for cell in map_data.get("cells", [])
        if cell.get("status") == "feasible" and cell.get("cell_id") is not None
    }
    patches = build_support_patches(map_data)
    rows = []
    for patch in patches:
        patch_id = str(patch.get("patch_id") or patch.get("support_plane_id") or "")
        if not patch_id:
            continue
        patch_surface_types = set(str(value) for value in patch.get("surface_types") or [])
        patch_surface_objects = [str(value) for value in patch.get("surface_objects") or []]
        floor_fallback = "floor" in patch_surface_types
        matches_required = bool(required_types & patch_surface_types)
        if matches_required:
            surface_role = "matched_surface"
            role_rank = 0
        elif floor_fallback:
            surface_role = "floor_fallback"
            role_rank = 2
        else:
            surface_role = "related_surface"
            role_rank = 1
        anchor_modes = _patch_variants(patch, placement_mode)
        candidate_modes = []
        for mode in anchor_modes:
            candidate_modes.append(mode)
        if placement_mode and placement_mode not in candidate_modes:
            candidate_modes.insert(0, placement_mode)
        for mode in candidate_modes:
            anchor_cell_id = patch_anchor_cell_id(patch, mode)
            anchor_cell = cells.get(str(anchor_cell_id)) if anchor_cell_id else None
            if anchor_cell is None:
                continue
            semantic_label = str(
                anchor_cell.get("semantic_label")
                or anchor_cell.get("semantic_cell_id")
                or patch.get("patch_label")
                or "/".join(part for part in (
                    patch_id or None,
                    str(anchor_cell.get("cell_id") or "") or None,
                ) if part)
                or anchor_cell.get("cell_id") or "")
            rows.append({
                "candidate_id": None,
                "screen_position": list(map(float, anchor_cell["screen_position"])),
                "cell_id": anchor_cell.get("cell_id"),
                "semantic_cell_id": anchor_cell.get("semantic_cell_id"),
                "column": anchor_cell.get("column"),
                "row": anchor_cell.get("row"),
                "region_id": anchor_cell.get("region_id"),
                "support_plane_id": anchor_cell.get("support_plane_id"),
                "support_patch_id": patch_id,
                "patch_id": patch_id,
                "patch_kind": patch.get("support_kind"),
                "patch_label": patch.get("patch_label"),
                "placement_mode": mode,
                "placement_modes": patch.get("placement_modes"),
                "anchor_cell_id": anchor_cell_id,
                "screen_polygon": patch.get("screen_polygon"),
                "world_xy_polygon": patch.get("world_xy_polygon"),
                "screen_bounds": patch.get("screen_bounds"),
                "world_xy_bounds": patch.get("world_xy_bounds"),
                "semantic_label": semantic_label,
                "surface_role": surface_role,
                "surface_role_rank": role_rank,
                "surface_types": sorted(patch_surface_types),
                "surface_objects": patch_surface_objects,
                "hit_surface": str(patch_surface_objects[0]) if patch_surface_objects else None,
                "plane_quality_score": patch.get("median_plane_quality_score"),
                "clearance_steps": patch.get("max_clearance_steps"),
                "world_position": anchor_cell.get("world_position"),
                "candidate_world_position": anchor_cell.get("candidate_world_position"),
                "surface_normal": anchor_cell.get("surface_normal"),
                "slope_deg": anchor_cell.get("slope_deg"),
                "feasible_yaws": anchor_cell.get("feasible_yaws"),
                "floor_fallback": floor_fallback,
                "region_rank": str(anchor_cell.get("region_id") or ""),
                "source_map_json": str(map_row["map_json"]),
                "edit_id": str(edit_id),
                "instance_index": None,
            })
    # Prefer the mode requested by the composer, then better patches, then
    # the deterministic floor fallback.
    def _mode_rank(mode: str, row: dict) -> tuple:
        desired = str(placement_mode or "center")
        if mode == desired:
            mode_rank = 0
        elif desired == "back_to_wall" and mode in {"back_to_wall", "row"}:
            mode_rank = 1
        elif desired == "row" and mode in {"row", "center"}:
            mode_rank = 1
        elif mode == "center":
            mode_rank = 2
        elif mode == "edge":
            mode_rank = 3
        else:
            mode_rank = 4
        return (
            mode_rank,
            int(row["surface_role_rank"]),
            -float(row["plane_quality_score"] or 0.0),
            -int(row["clearance_steps"] or 0),
            str(row["support_patch_id"] or row["support_plane_id"] or ""),
            str(row["cell_id"] or ""),
        )

    rows.sort(key=lambda row: _mode_rank(str(row.get("placement_mode") or "center"), row))
    for index, row in enumerate(rows, start=1):
        if not row.get("semantic_cell_id"):
            row["semantic_cell_id"] = f"S{index:03d}"
        if not str(row.get("semantic_label") or "").startswith("S"):
            row["semantic_label"] = row["semantic_cell_id"]
        row["placement_mode"] = str(row.get("placement_mode") or placement_mode or "center")
    if count <= 0:
        return []
    page_size = max(1, int(count))
    batch = max(0, int(batch))
    preferred = [row for row in rows if not row["floor_fallback"]]
    floor = [row for row in rows if row["floor_fallback"]]
    if preferred:
        preferred_pages = int(math.ceil(len(preferred) / page_size))
        if batch < preferred_pages:
            start = batch * page_size
            return preferred[start:start + page_size]
        floor_start = (batch - preferred_pages) * page_size
        return floor[floor_start:floor_start + page_size]
    start = batch * page_size
    return floor[start:start + page_size]


def build_candidate_plan(plan: dict, *, edit_id: str, instance_index: int,
                         count: int, batch: int,
                         semantic_map_index: Path | None = None,
                         points_override=None) -> tuple[dict, dict]:
    from placement_contract import (
        normalize_placement_spec,
        normalize_screen_positions,
        normalize_surface_policy,
    )

    candidate_plan = deepcopy(plan)
    matches = [
        edit for edit in candidate_plan.get("hero_edits", [])
        if edit.get("op") == "add" and str(edit.get("code_from")) == str(edit_id)
    ]
    if len(matches) != 1:
        raise ValueError(f"expected one add edit_id {edit_id!r}, found {len(matches)}")
    edit = matches[0]
    original_count = int(edit.get("n", 0))
    if not (0 <= int(instance_index) < original_count):
        raise ValueError(
            f"instance_index {instance_index} is outside 0..{original_count - 1}")
    spec = normalize_placement_spec(
        edit.get("placement_spec"), count=original_count,
        anchoring=edit.get("anchoring"))
    original_points = normalize_screen_positions(
        edit.get("screen_positions"), spec=spec, count=original_count)
    original_yaws = list(edit.get("placement_yaws") or [])
    fixed = [
        (index, point) for index, point in enumerate(original_points)
        if index != int(instance_index)
    ]
    candidate_rows = []
    candidate_surface_policy = None
    if semantic_map_index is not None:
        semantic_policy = spec["surface_policy"]
        fallback = edit.get("surface_fallback") or {}
        if isinstance(fallback, dict) and fallback.get("original_surface_policy"):
            semantic_policy = fallback["original_surface_policy"]
        candidate_rows = semantic_candidate_rows(
            map_index=semantic_map_index, edit_id=str(edit_id),
            count=count, batch=batch, surface_policy=semantic_policy,
            semantic_target=spec["semantic_target"],
            placement_mode=str(spec.get("placement_mode") or "center"))
        candidate_surface_policy = normalize_surface_policy(semantic_policy)
        candidate_surface_types = sorted({
            str(value) for row in candidate_rows
            for value in (row.get("surface_types") or [])
        })
        candidate_surface_names = sorted({
            str(row.get("hit_surface")) for row in candidate_rows
            if row.get("hit_surface")
        })
        if candidate_surface_types:
            candidate_surface_policy["required_surface_types"] = (
                candidate_surface_types)
        if candidate_surface_names:
            candidate_surface_policy["required_surface_names"] = (
                candidate_surface_names)
    elif points_override is not None:
        for candidate_index, item in enumerate(points_override):
            if isinstance(item, dict):
                point = item.get("screen_position")
                if point is None:
                    continue
                candidate_id = str(
                    item.get("candidate_id")
                    or f"B{int(instance_index)}-C{candidate_index}")
                row = {
                    "candidate_id": candidate_id,
                    "edit_id": str(edit_id),
                    "instance_index": int(instance_index),
                    "screen_position": list(map(float, point)),
                    "temporary_instance_index": len(fixed) + candidate_index,
                }
                for key in (
                        "cell_id", "region_id", "hit_surface",
                        "semantic_cell_id", "support_plane_id", "preview_label",
                        "surface_role", "surface_types", "plane_quality_score",
                        "clearance_steps", "world_position",
                        "candidate_world_position", "surface_normal",
                        "slope_deg", "feasible_yaws", "semantic_score",
                        "region_rank", "floor_fallback"):
                    if key in item:
                        row[key] = item[key]
                candidate_rows.append(row)
            else:
                point = list(map(float, item))
                candidate_rows.append({
                    "candidate_id": f"B{int(instance_index)}-C{candidate_index}",
                    "edit_id": str(edit_id),
                    "instance_index": int(instance_index),
                    "screen_position": point,
                    "temporary_instance_index": len(fixed) + candidate_index,
                })
    else:
        for candidate_index, point in enumerate(candidate_points(
                spec["screen_bounds"], count=count, batch=batch,
                anchoring=edit.get("anchoring"))):
            candidate_rows.append({
                "candidate_id": f"B{int(instance_index)}-C{candidate_index}",
                "edit_id": str(edit_id),
                "instance_index": int(instance_index),
                "screen_position": point,
                "temporary_instance_index": len(fixed) + candidate_index,
            })
    if not candidate_rows:
        raise ValueError("candidate point list is empty")
    for candidate_index, row in enumerate(candidate_rows):
        row["candidate_id"] = f"B{int(instance_index)}-C{candidate_index}"
        row["edit_id"] = str(edit_id)
        row["instance_index"] = int(instance_index)
        row["preview_label"] = str(
            row.get("preview_label")
            or row.get("semantic_label")
            or row.get("support_plane_id")
            or row.get("region_id")
            or row.get("cell_id")
            or row["candidate_id"])
        row["temporary_instance_index"] = len(fixed) + candidate_index
    temporary_points = [point for _index, point in fixed] + [
        row["screen_position"] for row in candidate_rows]
    temporary_labels = [
        f"{str(edit_id).replace('_', ' ').upper()} #{index} - FIXED"
        for index, _point in fixed
    ]
    temporary_yaws = [
        float(original_yaws[index]) if index < len(original_yaws)
        and original_yaws[index] is not None else 0.0
        for index, _point in fixed
    ]
    rows = []
    for row in candidate_rows:
        temporary_labels.append(row["candidate_id"])
        feasible_yaws = row.get("feasible_yaws") or []
        temporary_yaws.append(
            float(feasible_yaws[0]) if feasible_yaws else 0.0)
        rows.append(row)

    edit["n"] = len(temporary_points)
    edit["n_masters"] = max(int(edit.get("n_masters") or 1), len(temporary_points))
    if len(temporary_points) > 1 and edit.get("placement_spec", {}).get(
            "arrangement") == "single":
        edit["placement_spec"]["arrangement"] = "scatter"
    if candidate_surface_policy is not None:
        edit.setdefault("placement_spec", {})["surface_policy"] = (
            candidate_surface_policy)
    edit["screen_positions"] = temporary_points
    edit["placement_yaws"] = temporary_yaws
    edit["screen_position_projection"] = "visible_surface"
    edit["candidate_preview_allow_projection_misses"] = True
    edit["placement_preview_labels"] = temporary_labels
    semantic_world_positions = [
        row.get("candidate_world_position") or row.get("world_position")
        for row in candidate_rows
    ]
    if (semantic_map_index is not None and not fixed
            and all(position is not None for position in semantic_world_positions)):
        edit["positions"] = [
            list(map(float, position)) for position in semantic_world_positions]
        edit["explicit_position_supports"] = [
            explicit_support_record(row) for row in candidate_rows]
    else:
        edit.pop("positions", None)
        edit.pop("explicit_position_supports", None)
    edit["reason"] = (
        f"Temporary candidate sheet for {edit_id} instance {instance_index}; "
        "never populate this plan.")
    metadata = {
        "contract_version": 1,
        "edit_id": str(edit_id),
        "instance_index": int(instance_index),
        "semantic_target": spec["semantic_target"],
        "surface_policy": spec["surface_policy"],
        "candidate_surface_policy": candidate_surface_policy,
        "semantic_mode": bool(semantic_map_index is not None),
        "semantic_map_index": str(semantic_map_index) if semantic_map_index else None,
        "uses_explicit_world_positions": bool(edit.get("positions")),
        "candidate_count": len(rows),
        "batch": int(batch),
        "base_screen_positions": original_points,
        "fixed_instances": [
            {"instance_index": index, "screen_position": point,
             "temporary_instance_index": temporary_index}
            for temporary_index, (index, point) in enumerate(fixed)
        ],
        "candidates": rows,
    }
    return candidate_plan, metadata


def main() -> int:
    args = parse_args()
    if args.batch < 0:
        raise SystemExit("--batch must be >= 0")
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    points_override = None
    if args.points_file:
        probe = json.loads(args.points_file.read_text(encoding="utf-8"))
        points_override = probe.get("accepted", [])
    candidate_plan, metadata = build_candidate_plan(
        plan, edit_id=args.edit_id, instance_index=args.instance_index,
        count=args.count, batch=args.batch,
        semantic_map_index=args.semantic_map_index,
        points_override=points_override)
    args.output_plan.parent.mkdir(parents=True, exist_ok=True)
    args.output_candidates.parent.mkdir(parents=True, exist_ok=True)
    args.output_plan.write_text(
        json.dumps(candidate_plan, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    args.output_candidates.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(
        f"[placement-candidates] {args.edit_id}#{args.instance_index}: "
        f"{len(metadata['candidates'])} candidates -> {args.output_plan}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
