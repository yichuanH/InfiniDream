#!/usr/bin/env python
"""Render selected and previously excluded cells onto deterministic region maps."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

from render_placement_region_map import render_region_map
from placement_region_contract import cross_edit_reserved_cells


def load_application(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [dict(row) for row in payload.get("applied", [])]


def history_applications(root: Path | None, current: Path) -> list[dict]:
    if root is None or not root.is_dir():
        return []
    current_resolved = current.resolve()
    result = []
    def attempt_order(path: Path):
        if "region_map_initial" in path.parts:
            return (0, str(path))
        rounds = [re.fullmatch(r"round_(\d+)", part) for part in path.parts]
        numbers = [int(match.group(1)) for match in rounds if match]
        return ((numbers[-1] if numbers else -1), str(path))

    for path in sorted(root.rglob("region_application.json"), key=attempt_order):
        if path.resolve() == current_resolved:
            continue
        for row in load_application(path):
            row["source_application"] = str(path.resolve())
            result.append(row)
    return result


def active_selection_state(history: list[dict], current: list[dict]
                           ) -> tuple[list[dict], list[dict]]:
    """Return latest active cells and only the cells they replaced.

    The current application is always the newest attempt.  Historical rows are
    supplied in initial, round_01, round_02, ... chronological order.
    """
    latest = {}
    all_rows = []
    for row in [*history, *current]:
        if row.get("instance_index") is None or not row.get("edit_id"):
            continue
        copied = dict(row)
        key = (str(copied["edit_id"]), int(copied["instance_index"]))
        all_rows.append((key, copied))
        latest[key] = copied
    selected = [latest[key] for key in sorted(latest)]
    active_cells = {
        (str(row.get("edit_id")), str(row.get("cell_id"))) for row in selected
    }
    excluded = [row for _, row in all_rows
                if (str(row.get("edit_id")), str(row.get("cell_id")))
                not in active_cells]
    return selected, excluded


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--map-index", type=Path, required=True)
    parser.add_argument("--application", type=Path, required=True)
    parser.add_argument("--history-root", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--label", default="deterministic selection")
    args = parser.parse_args()

    index = json.loads(args.map_index.read_text(encoding="utf-8"))
    current = load_application(args.application)
    history = history_applications(args.history_root, args.application)
    selected, excluded = active_selection_state(history, current)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for map_row in index.get("maps", []):
        edit_id = str(map_row["edit_id"])
        map_path = args.map_index.parent / map_row["map_json"]
        data = json.loads(map_path.read_text(encoding="utf-8"))
        reserved = cross_edit_reserved_cells(data, selected)
        data["selection_overlay"] = {
            "label": args.label,
            "excluded": [row for row in excluded
                         if str(row.get("edit_id")) == edit_id],
            "selected": [row for row in selected
                         if str(row.get("edit_id")) == edit_id],
            "cross_edit_reserved": reserved,
        }
        output = args.output_dir / f"{edit_id}_selected_region_map.png"
        render_region_map(data, output)
        rows.append({
            "edit_id": edit_id,
            "selected_count": len(data["selection_overlay"]["selected"]),
            "excluded_count": len(data["selection_overlay"]["excluded"]),
            "cross_edit_reserved_count": len(reserved),
            "output": output.name,
        })
    annotation_index = {
        "contract_version": 1,
        "label": args.label,
        "map_index": str(args.map_index.resolve()),
        "application": str(args.application.resolve()),
        "rows": rows,
    }
    output_index = args.output_dir / "selected_region_map_index.json"
    output_index.write_text(
        json.dumps(annotation_index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    state_path = args.output_dir / "selected_region_cells.json"
    state_path.write_text(json.dumps({
        "contract_version": 1,
        "selection_policy": "arrangement_aware_deterministic_no_api",
        "active_selected_cells": selected,
        "excluded_replaced_cells": excluded,
        "cross_edit_reservations_by_map": {
            row["edit_id"]: row["cross_edit_reserved_count"] for row in rows},
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"PLACEMENT_REGION_MAP_ANNOTATED → {output_index}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
