#!/usr/bin/env python
"""Reduce a raycast-reviewed deterministic pool to dispersed valid candidates."""

from __future__ import annotations

import argparse
import json
import math
from copy import deepcopy
from pathlib import Path

from placement_contract import normalize_placement_spec, surface_policy_issue


def filter_candidates(metadata, review, policy, *, limit=10):
    row = next((x for x in review.get("placements", [])
                if str(x.get("code_from")) == str(metadata["edit_id"])), None)
    if row is None:
        raise ValueError(f"review has no placement for {metadata['edit_id']!r}")
    audits = {int(x.get("instance_index")): x for x in row.get("anchors", [])}
    fixed_indices = {int(x.get("temporary_instance_index", x["instance_index"]))
                     for x in metadata.get("fixed_instances", [])}
    fixed_names = {audits[i].get("object") for i in fixed_indices if i in audits}
    collision_names = set()
    for violation in review.get("violations") or []:
        objects = set(violation.get("objects") or [])
        if objects & fixed_names:
            collision_names |= objects - fixed_names
    valid, rejected = [], []
    for candidate in metadata.get("candidates", []):
        audit = audits.get(int(candidate["temporary_instance_index"]))
        reasons = []
        if audit is None:
            reasons.append("missing machine audit")
        else:
            issue = surface_policy_issue(
                policy, surface_name=audit.get("selected_visible_surface"),
                slope_deg=audit.get("selected_surface_slope_deg"))
            if issue:
                reasons.append(issue)
            if not audit.get("all_frames_fully_visible"):
                reasons.append("complete box leaves frame")
            if policy.get("require_full_footprint_support", True) and not (
                    audit.get("footprint_support") or {}).get("supported"):
                reasons.append("full footprint is not supported")
            if audit.get("object") in collision_names:
                reasons.append("3D world box collides with fixed sibling")
        item = deepcopy(candidate)
        if reasons:
            item["machine_issues"] = reasons
            rejected.append(item)
        else:
            valid.append(item)

    # Deterministic farthest-point sampling in screen space. Start with the
    # point farthest from fixed siblings, then maximize distance to selections.
    fixed_points = [x["screen_position"] for x in metadata.get("fixed_instances", [])]
    selected = []
    remaining = list(valid)
    while remaining and len(selected) < int(limit):
        references = fixed_points + [x["screen_position"] for x in selected]
        def score(item):
            point = item["screen_position"]
            distance = min((math.dist(point, ref) for ref in references), default=1e9)
            return distance, item["candidate_id"]
        chosen = max(remaining, key=score)
        remaining.remove(chosen)
        selected.append(chosen)
    return selected, rejected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--pool-plan", type=Path, required=True)
    parser.add_argument("--pool-candidates", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--output-plan", type=Path, required=True)
    parser.add_argument("--output-candidates", type=Path, required=True)
    args = parser.parse_args()
    original = json.loads(args.plan.read_text())
    pool_plan = json.loads(args.pool_plan.read_text())
    metadata = json.loads(args.pool_candidates.read_text())
    review = json.loads(args.review.read_text())
    edit = next(x for x in original["hero_edits"]
                if x.get("op") == "add" and str(x.get("code_from")) == metadata["edit_id"])
    spec = normalize_placement_spec(edit.get("placement_spec"), count=int(edit["n"]),
                                    anchoring=edit.get("anchoring"))
    selected, rejected = filter_candidates(
        metadata, review, spec["surface_policy"], limit=args.count)
    final_plan = deepcopy(pool_plan)
    target = next(x for x in final_plan["hero_edits"]
                  if x.get("op") == "add" and str(x.get("code_from")) == metadata["edit_id"])
    fixed = metadata.get("fixed_instances", [])
    target["n"] = len(fixed) + len(selected)
    target["screen_positions"] = [x["screen_position"] for x in fixed + selected]
    target["placement_preview_labels"] = [
        f"{metadata['edit_id'].replace('_', ' ').upper()} #{x['instance_index']} - FIXED"
        for x in fixed] + [x["candidate_id"] for x in selected]
    for index, item in enumerate(selected, start=len(fixed)):
        item["temporary_instance_index"] = index
    output = deepcopy(metadata)
    output["contract_version"] = 2
    output["pool_candidate_count"] = len(metadata.get("candidates", []))
    output["candidate_count"] = len(selected)
    output["candidates"] = selected
    output["rejected_candidates"] = rejected
    args.output_plan.write_text(json.dumps(final_plan, indent=2) + "\n")
    args.output_candidates.write_text(json.dumps(output, indent=2) + "\n")
    if not selected:
        decision_path = args.output_candidates.parent / "candidate_decision.json"
        decision_path.write_text(json.dumps({
            "decision": "none", "candidate_id": None,
            "reason": "No deterministic pool point survived the machine hard constraints.",
            "candidate_payload": output,
        }, indent=2) + "\n")
        print(f"[candidate-filter] 0/{output['pool_candidate_count']} retained")
        raise SystemExit(2)
    print(f"[candidate-filter] {len(selected)}/{output['pool_candidate_count']} retained")


if __name__ == "__main__":
    main()
