#!/usr/bin/env python
"""Apply deterministic feasible-region choices as exact screen points."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from placement_region_contract import (
    all_map_managed_instances,
    all_conform_instances,
    apply_region_selection,
    automatic_region_assignments,
    conform_failed_instances,
    map_managed_failed_instances,
    validate_region_selection,
)


def load_maps(index_path: Path) -> dict[str, dict]:
    index = json.loads(index_path.read_text(encoding="utf-8"))
    return {
        str(row["edit_id"]): json.loads(
            (index_path.parent / row["map_json"]).read_text(encoding="utf-8"))
        for row in index.get("maps", [])
    }


def rejected_history(history_root: Path | None) -> dict[tuple[str, int], list[list[float]]]:
    result: dict[tuple[str, int], list[list[float]]] = {}
    if history_root is None or not history_root.is_dir():
        return result
    seen = set()
    for application_path in sorted(history_root.rglob("region_application.json")):
        payload = json.loads(application_path.read_text(encoding="utf-8"))
        for row in payload.get("applied", []):
            edit_id = str(row.get("edit_id") or "")
            index = row.get("instance_index")
            point = row.get("screen_position_after")
            if not edit_id or index is None or not isinstance(point, list) or len(point) != 2:
                continue
            normalized = tuple(map(float, point))
            key = (edit_id, int(index), normalized)
            if key in seen:
                continue
            seen.add(key)
            result.setdefault((edit_id, int(index)), []).append(list(normalized))
    for round_dir in sorted(history_root.rglob("round_*")):
        for filename in ("edit_plan_before.json", "edit_plan_after.json"):
            path = round_dir / filename
            if not path.is_file():
                continue
            plan = json.loads(path.read_text(encoding="utf-8"))
            for edit in plan.get("hero_edits", []):
                edit_id = str(edit.get("code_from") or "")
                for index, point in enumerate(edit.get("screen_positions") or []):
                    if edit_id and isinstance(point, list) and len(point) == 2:
                        normalized = tuple(map(float, point))
                        key = (edit_id, index, normalized)
                        if key not in seen:
                            seen.add(key)
                            result.setdefault((edit_id, index), []).append(
                                list(normalized))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--review", type=Path)
    parser.add_argument("--map-index", type=Path, required=True)
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--selection-output", type=Path)
    parser.add_argument("--auto-select", action="store_true")
    parser.add_argument("--all-conform", action="store_true")
    parser.add_argument("--all-map-managed", action="store_true")
    parser.add_argument("--history-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--application", type=Path, required=True)
    parser.add_argument("--infeasibility-output", type=Path)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    maps = load_maps(args.map_index)
    if args.all_conform and args.all_map_managed:
        raise ValueError("choose only one all-instance map scope")
    if args.all_map_managed:
        expected = all_map_managed_instances(plan)
    elif args.all_conform:
        expected = all_conform_instances(plan)
    else:
        if args.review is None:
            raise ValueError(
                "--review is required unless an all-instance scope is used")
        review = json.loads(args.review.read_text(encoding="utf-8"))
        expected = map_managed_failed_instances(plan, review)
    rejected = rejected_history(args.history_root)
    try:
        if args.auto_select:
            proposed = automatic_region_assignments(
                plan, maps=maps, expected=expected,
                rejected_positions=rejected)
            selection = {
                "contract_version": 1,
                "decision": "select_regions",
                "selection_policy": "arrangement_aware_deterministic_no_api",
                "assignments": proposed,
            }
        else:
            if args.selection is None:
                raise ValueError(
                    "--selection is required unless --auto-select is used")
            selection = json.loads(args.selection.read_text(encoding="utf-8"))
        assignments = validate_region_selection(
            selection, maps=maps, expected=expected)
        revised, application = apply_region_selection(
            plan,
            maps=maps,
            assignments=assignments,
            rejected_positions=rejected,
        )
    except ValueError as error:
        if args.infeasibility_output is not None:
            args.infeasibility_output.parent.mkdir(parents=True, exist_ok=True)
            args.infeasibility_output.write_text(json.dumps({
                "contract_version": 1,
                "status": "INFEASIBLE",
                "selection_policy": "deterministic_no_api",
                "reason": str(error),
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"PLACEMENT_REGION_SELECTION_INFEASIBLE: {error}")
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.application.parent.mkdir(parents=True, exist_ok=True)
    if args.selection_output is not None:
        args.selection_output.parent.mkdir(parents=True, exist_ok=True)
        args.selection_output.write_text(
            json.dumps(selection, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    payload = json.dumps(revised, ensure_ascii=False, indent=2) + "\n"
    args.output.write_text(payload, encoding="utf-8")
    args.plan.write_text(payload, encoding="utf-8")
    application["selection_policy"] = selection.get(
        "selection_policy", "external_region_selection")
    application["assignments"] = assignments
    args.application.write_text(
        json.dumps(application, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(f"PLACEMENT_REGION_APPLIED → {args.plan}")
    for row in application["applied"]:
        print(
            f"  {row['edit_id']}#{row['instance_index']} "
            f"{row['region_id']}/{row['cell_id']} → {row['screen_position_after']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
