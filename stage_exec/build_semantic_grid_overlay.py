#!/usr/bin/env python
"""Flatten region-map cells into one semantic overlay JSON for render_view."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import surface_registry as sr
from semantic_support_patches import build_support_patches, selected_patch_ids


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--map-index", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--edit-id")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--selection-cells", type=Path, action="append",
                        default=[])
    argv = sys.argv[1:]
    if argv and argv[0] == "--":
        argv = argv[1:]
    return parser.parse_args(argv)


def _edit_labels(plan_path: Path | None) -> dict[str, str]:
    if plan_path is None or not plan_path.is_file():
        return {}
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    labels = {}
    for edit in plan.get("hero_edits", []):
        if edit.get("op") != "add":
            continue
        edit_id = str(edit.get("code_from") or "")
        if not edit_id:
            continue
        spec = edit.get("placement_spec") or {}
        label = " ".join(part for part in (
            str(edit.get("factory") or "").replace("Factory", ""),
            str(spec.get("semantic_target") or ""),
        ) if part).strip()
        labels[edit_id] = label or edit_id.replace("_", " ")
    return labels


def _load_selected_rows(paths: list[Path], labels: dict[str, str]) -> list[dict]:
    rows = []
    for path in paths:
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            source_rows = payload
        elif isinstance(payload.get("active_selected_cells"), list):
            source_rows = payload["active_selected_cells"]
        elif isinstance(payload.get("applied"), list):
            source_rows = payload["applied"]
        elif isinstance(payload.get("selected"), list):
            source_rows = payload["selected"]
        else:
            source_rows = []
        for row in source_rows:
            if not isinstance(row, dict):
                continue
            copied = dict(row)
            edit_id = str(copied.get("edit_id") or "")
            object_label = str(
                copied.get("object_label")
                or copied.get("selected_label")
                or labels.get(edit_id)
                or edit_id.replace("_", " "))
            copied["object_label"] = object_label
            copied["source_selection"] = str(path)
            rows.append(copied)
    return rows


def _selection_matches(cell: dict, selected: dict) -> bool:
    has_precise_key = False
    for key in ("cell_id", "semantic_cell_id"):
        selected_value = selected.get(key)
        if selected_value is None:
            continue
        has_precise_key = True
        if str(cell.get(key) or "") == str(selected_value):
            return True
    if has_precise_key:
        return False
    screen_after = selected.get("screen_position_after")
    screen = cell.get("screen_position")
    if (isinstance(screen_after, list) and len(screen_after) == 2
            and isinstance(screen, list) and len(screen) == 2):
        return (
            abs(float(screen[0]) - float(screen_after[0])) < 1e-5
            and abs(float(screen[1]) - float(screen_after[1])) < 1e-5)
    for key in ("support_plane_id", "region_id"):
        selected_value = selected.get(key)
        if selected_value is None:
            continue
        if str(cell.get(key) or "") == str(selected_value):
            return True
    for key in ("support_patch_id", "patch_id"):
        selected_value = selected.get(key)
        if selected_value is None:
            continue
        if str(cell.get("support_plane_id") or cell.get("region_id") or "") == str(selected_value):
            return True
    return False


def main() -> int:
    args = parse_args()
    index = json.loads(args.map_index.read_text(encoding="utf-8"))
    edit_labels = _edit_labels(args.plan)
    selected_rows = _load_selected_rows(args.selection_cells, edit_labels)
    scene_name = str(index.get("scene_name") or index.get("scene") or "")
    scene_location = str(index.get("scene_location") or "")
    rows = []
    surface_inventory = []
    source_rows = []
    support_grid = index.get("semantic_support_grid")
    if isinstance(support_grid, dict) and support_grid.get("map_json"):
        source_rows.append(dict(support_grid, edit_id="__semantic_support_grid"))
    else:
        source_rows.extend(index.get("maps", []))
    for map_row in source_rows:
        edit_id = str(map_row.get("edit_id") or "")
        if args.edit_id and edit_id not in {args.edit_id, "__semantic_support_grid"}:
            continue
        map_path = args.map_index.parent / map_row["map_json"]
        map_data = json.loads(map_path.read_text(encoding="utf-8"))
        grid = map_data.get("grid") or {}
        columns = int(grid.get("columns") or 1)
        rows_n = int(grid.get("rows") or 1)
        surface_inventory.extend(
            dict(entry, edit_id=edit_id)
            for entry in map_data.get("surface_inventory") or [])
        for cell in map_data.get("cells", []):
            if cell.get("status") != "feasible":
                continue
            hit_surface = str(cell.get("hit_surface") or "")
            support_plane = str(cell.get("support_plane_id") or "")
            region_id = str(cell.get("region_id") or "")
            semantic_label = str(
                cell.get("semantic_label")
                or "/".join(part for part in (
                    support_plane or None,
                    str(cell.get("cell_id") or "") or None,
                ) if part)
                or region_id
                or cell.get("cell_id")
                or "")
            rows.append({
                "edit_id": edit_id,
                "screen_bounds": map_data.get("screen_bounds"),
                "grid_columns": columns,
                "grid_rows": rows_n,
                "semantic_cell_id": cell.get("semantic_cell_id"),
                "cell_id": cell.get("cell_id"),
                "column": cell.get("column"),
                "row": cell.get("row"),
                "screen_position": cell.get("screen_position"),
                "screen_polygon": cell.get("screen_polygon"),
                "region_id": region_id,
                "support_plane_id": support_plane,
                "semantic_label": semantic_label,
                "status": cell.get("status"),
                "hit_surface": hit_surface,
                "surface_types": sr.types_for_mesh(scene_name, hit_surface),
                "plane_quality_score": cell.get("plane_quality_score"),
                "clearance_steps": cell.get("clearance_steps"),
                "world_position": cell.get("world_position"),
                "candidate_world_position": cell.get("candidate_world_position"),
                "surface_normal": cell.get("surface_normal"),
                "slope_deg": cell.get("slope_deg"),
                "world_xy_polygon": cell.get("world_xy_polygon"),
                "semantic_source": cell.get("semantic_source"),
            })
    for semantic_index, row in enumerate(sorted(rows, key=lambda item: (
            str(item.get("support_plane_id") or ""),
            str(item.get("cell_id") or ""))), start=1):
        if not row.get("semantic_cell_id"):
            row["semantic_cell_id"] = f"S{semantic_index:03d}"
        if not str(row.get("semantic_label") or "").startswith("S"):
            row["semantic_label"] = row["semantic_cell_id"]
    patch_payload = {
        "cells": rows,
        "surface_inventory": surface_inventory,
    }
    patches = build_support_patches(patch_payload)
    selected_patch_id_set = selected_patch_ids(selected_rows)
    matched_selected = []
    for row in rows:
        matches = [
            selected for selected in selected_rows
            if _selection_matches(row, selected)
        ]
        if not matches:
            continue
        selected = matches[0]
        row["selected"] = True
        row["selected_label"] = selected.get("object_label")
        row["selected_edit_id"] = selected.get("edit_id")
        row["selected_instance_index"] = selected.get("instance_index")
        matched_selected.append({
            "semantic_cell_id": row.get("semantic_cell_id"),
            "cell_id": row.get("cell_id"),
            "support_plane_id": row.get("support_plane_id"),
            "support_patch_id": row.get("support_patch_id")
            or row.get("support_plane_id"),
            "patch_kind": row.get("patch_kind") or row.get("surface_types"),
            "placement_mode": row.get("placement_mode"),
            "region_id": row.get("region_id"),
            "object_label": selected.get("object_label"),
            "edit_id": selected.get("edit_id"),
            "instance_index": selected.get("instance_index"),
        })
    selected_patch_id_set.update(
        str(row.get("support_patch_id") or row.get("support_plane_id") or row.get("patch_id") or "")
        for row in matched_selected
        if (row.get("support_patch_id") or row.get("support_plane_id")
            or row.get("patch_id"))
    )
    selected_patches = []
    for patch in patches:
        if str(patch.get("support_plane_id") or patch.get("patch_id") or "") \
                not in selected_patch_id_set:
            continue
        selected_patches.append({
            "patch_id": patch.get("patch_id"),
            "support_plane_id": patch.get("support_plane_id"),
            "support_kind": patch.get("support_kind"),
            "patch_label": patch.get("patch_label"),
            "placement_modes": patch.get("placement_modes"),
            "selected_label": next(
                (row.get("object_label") for row in matched_selected
                 if str(row.get("support_plane_id") or "") == str(
                     patch.get("support_plane_id") or patch.get("patch_id") or "")),
                None,
            ),
        })
    payload = {
        "contract_version": 1,
        "mode": "semantic_grid_overlay",
        "scene_name": scene_name,
        "scene_location": scene_location,
        "screen_bounds": index.get("screen_bounds"),
        "grid": index.get("grid"),
        "cells": rows,
        "surface_inventory": surface_inventory,
        "patches": patches,
        "selected_patches": selected_patches,
        "selected_cells": matched_selected,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(f"[semantic-grid-overlay] {len(rows)} cells -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
